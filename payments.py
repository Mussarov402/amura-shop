"""Деньги: оплаты, выплаты и корректировки долга из системы AMURA с двойной записью в МойСклад (как whops для склада).

- операция сначала сохраняется у нас (таблица pay_op, статус queued), затем сразу отправляется в МойСклад;
  externalCode документа = id операции;
- оплата от контрагента: на счёт — входящий платёж (paymentin), в кассу — приходный ордер (cashin);
  выплата контрагенту: со счёта — исходящий платёж (paymentout), из кассы — расходный ордер (cashout), обязательно со статьёй расходов;
  корректировка взаиморасчётов (counterpartyadjustment): сумма со знаком, как в отчёте взаиморасчётов —
  плюс увеличивает «мы должны» (или уменьшает «нам должны»), минус — наоборот (проверено на корректировках 10.10);
- сбой сети или 5xx — повтор в фоне, перед повтором поиск документа по externalCode (второго платежа не будет);
  4xx — статус error, владелец видит причину и жмёт «Повторить»;
- после проводки баланс контрагента и остатки денег сразу перечитываются из МойСклад (лёгкие отчёты, только чтение),
  не дожидаясь прохода зеркала.
Только владелец (маршруты под @guard). Работает при включённом модуле «Финансы»; выключатель — флаг feat_pay_ops."""
import os
import threading
import time
import uuid
from datetime import datetime, timedelta

import inbox
import mirror
import modules
import order_hook as oh

FLAG = "feat_pay_ops"
KINDS = {"paymentin": "Входящий платёж", "paymentout": "Исходящий платёж", "cashin": "Приходный ордер",
         "cashout": "Расходный ордер", "counterpartyadjustment": "Корректировка долга"}
DIRS = {"in": "Оплата", "out": "Выплата", "adj": "Корректировка"}
CASH = "cash"
MAX_SUM = 10 ** 10          # ₸, защита от лишних нулей
RETRY_EVERY = 60
MAX_AUTO = 30
_ready = [False]
_lock = threading.Lock()
_meta = {}


def _schema(d):
    if _ready[0]:
        return
    d.run("CREATE TABLE IF NOT EXISTS pay_op (id TEXT PRIMARY KEY, kind TEXT, agent_id TEXT, sum DOUBLE PRECISION, account TEXT,"
          " expense TEXT, moment TEXT, descr TEXT, status TEXT, ms_id TEXT, ms_number TEXT, attempts INTEGER DEFAULT 0, error TEXT,"
          " who TEXT, created DOUBLE PRECISION, sent DOUBLE PRECISION, next_try DOUBLE PRECISION DEFAULT 0)")
    d.c.commit()
    _ready[0] = True


def db():
    d = inbox.db()
    _schema(d)
    return d


def enabled():
    if not modules.enabled("finance"):
        return False
    with inbox.db() as d:
        return inbox.get_setting(d, FLAG, "1") == "1"     # решение владельца 10.10.2026: готовое включаем сразу, выключатель в «Деньгах»


def set_enabled(on):
    with inbox.db() as d:
        inbox.set_setting(d, FLAG, "1" if on else "0")


def meta():
    """Счета организации, касса и статьи расходов из МойСклад (кэш 10 минут)."""
    c = _meta.get("v")
    if not c or time.time() - c[0] > 600:
        org = oh.organization()
        accs = oh.ms("GET", f"/entity/organization/{org}/accounts", timeout=30)
        accs = accs if isinstance(accs, list) else accs.get("rows", [])
        items = oh.ms("GET", "/entity/expenseitem", params={"limit": 1000}, timeout=30)
        items = items if isinstance(items, list) else items.get("rows", [])
        accounts = [{"id": a.get("id") or "", "main": bool(a.get("isDefault")),
                     "name": (a.get("bankName") or "Расчётный счёт") + (" · …" + a["accountNumber"][-4:] if a.get("accountNumber") else "")} for a in accs]
        accounts.sort(key=lambda a: not a["main"])
        c = (time.time(), {"accounts": accounts + [{"id": CASH, "name": "Касса (наличные)", "main": False}],
                           "expenses": sorted([{"id": x.get("id") or "", "name": x.get("name") or ""} for x in items if not x.get("archived")],
                                              key=lambda x: x["name"])})
        _meta["v"] = c
    return c[1]


def _row(r):
    i, kind, agent, s, acc, exp, moment, descr, status, ms_id, ms_num, att, err, who, created, sent = r[:16]
    return {"id": i, "kind": kind, "title": KINDS.get(kind, kind), "agent": agent, "sum": s or 0, "account": acc or "", "expense": exp or "",
            "moment": moment or "", "descr": descr or "", "status": status, "msId": ms_id or "", "msNumber": ms_num or "",
            "attempts": att or 0, "error": err or "", "who": who or "", "created": created or 0, "sent": sent or 0}


_COLS = "id, kind, agent_id, sum, account, expense, moment, descr, status, ms_id, ms_number, attempts, error, who, created, sent"


def get(op_id):
    with db() as d:
        r = d.run(f"SELECT {_COLS} FROM pay_op WHERE id=%s", (op_id,), one=True)
    return _row(r) if r else None


def recent(limit=20, agent_id=""):
    with db() as d:
        if agent_id:
            rows = d.run(f"SELECT {_COLS} FROM pay_op WHERE agent_id=%s ORDER BY created DESC LIMIT %s", (agent_id, limit), many=True) or []
        else:
            rows = d.run(f"SELECT {_COLS} FROM pay_op ORDER BY created DESC LIMIT %s", (limit,), many=True) or []
        ids = list({r[2] for r in rows if r[2]})
        names = dict(d.run("SELECT id, name FROM ms_agent WHERE id IN (" + ", ".join(["%s"] * len(ids)) + ")", ids, many=True) or []) if ids else {}
    out = []
    for r in rows:
        o = _row(r)
        o["agentName"] = names.get(o["agent"], "")
        out.append(o)
    return out


def _moment(day):
    """Дата (Алматы) → время МойСклад (Москва). Сегодня — текущее время, другой день — полдень по Алматы."""
    now = datetime.now(oh.ALMATY)
    if not day or day == str(now.date()):
        t = now
    else:
        try:
            dd = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("Дата — в формате ГГГГ-ММ-ДД")
        if dd > now.date():
            raise ValueError("Дата не может быть в будущем")
        if dd < now.date() - timedelta(days=366):
            raise ValueError("Дата — не раньше, чем год назад")
        t = datetime(dd.year, dd.month, dd.day, 12, 0, tzinfo=oh.ALMATY)
    return t.astimezone(mirror.MS_TZ).strftime("%Y-%m-%d %H:%M:%S")


def create(direction, agent_id, amount, account="", expense="", day="", descr="", who=""):
    """Проверяет и сохраняет операцию, затем сразу пробует провести в МойСклад. Ошибка проверки — ValueError."""
    if direction not in DIRS:
        raise ValueError("Неизвестная операция")
    try:
        s = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("Сумма — число")
    if direction == "adj" and not s or direction != "adj" and s <= 0:
        raise ValueError("Сумма должна быть больше нуля" if direction != "adj" else "Сумма корректировки не может быть нулём")
    if abs(s) > MAX_SUM:
        raise ValueError("Слишком большая сумма — проверьте нули")
    descr = (descr or "").strip()[:500]
    with mirror.db() as d:
        if not d.run("SELECT 1 FROM ms_agent WHERE id=%s AND deleted=0", (agent_id or "",), one=True):
            raise ValueError("Выберите контрагента")
    moment = _moment(day)
    if direction == "adj":
        kind, account, expense = "counterpartyadjustment", "", ""
    else:
        m = meta()
        if account not in {a["id"] for a in m["accounts"]}:
            raise ValueError("Выберите счёт или кассу")
        if direction == "out":
            if expense not in {x["id"] for x in m["expenses"]}:
                raise ValueError("Выберите статью расходов")
        else:
            expense = ""
        kind = {("in", False): "paymentin", ("in", True): "cashin", ("out", False): "paymentout", ("out", True): "cashout"}[(direction, account == CASH)]
    op_id = str(uuid.uuid4())
    with db() as d:
        d.run(f"INSERT INTO pay_op ({_COLS}, next_try) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', '', '', 0, '', %s, %s, 0, 0)",
              (op_id, kind, agent_id, s, account, expense, moment, descr, who, time.time()))
    send(op_id)
    return get(op_id)


def _body(o):
    org = oh.organization()
    body = {"externalCode": o["id"], "organization": oh.meta("organization", org), "agent": oh.meta("counterparty", o["agent"]),
            "moment": o["moment"], "applicable": True, "sum": round(o["sum"] * 100),
            "description": ("Из системы AMURA" + (f" ({o['who']})" if o["who"] else "") + (": " + o["descr"] if o["descr"] else ""))[:4000]}
    if o["kind"] != "counterpartyadjustment":
        body["paymentPurpose"] = o["descr"][:255]
    if o["kind"] in ("paymentin", "paymentout") and o["account"] and o["account"] != CASH:
        body["organizationAccount"] = {"meta": {"href": f"{oh.API}/entity/organization/{org}/accounts/{o['account']}",
                                                "type": "account", "mediaType": "application/json"}}
    if o["kind"] in ("paymentout", "cashout"):
        body["expenseItem"] = oh.meta("expenseitem", o["expense"])
    return body


def _mark(op_id, **f):
    sets = ", ".join(f"{k}=%s" for k in f)
    with db() as d:
        d.run(f"UPDATE pay_op SET {sets} WHERE id=%s", (*f.values(), op_id))


def send(op_id):
    """Одна попытка провести операцию. Никогда не создаёт второй документ в МойСклад."""
    with _lock:
        o = get(op_id)
        if not o or o["status"] == "sent":
            return o
        att = o["attempts"] + 1
        _mark(op_id, attempts=att)
        try:
            row = None
            if o["attempts"]:
                found = oh.ms("GET", f"/entity/{o['kind']}", params={"filter": f"externalCode={o['id']}", "limit": 1}, timeout=20).get("rows") or []
                row = found[0] if found else None
            if row is None:
                row = oh.ms("POST", f"/entity/{o['kind']}", json=_body(o), timeout=30)
            _mark(op_id, status="sent", ms_id=row.get("id") or "", ms_number=row.get("name") or "", error="", sent=time.time())
            print(f"Деньги: «{KINDS[o['kind']]}» {op_id[:8]} на {o['sum']:.0f} ₸ проведён в МойСклад №{row.get('name')}", flush=True)
            _bg(refresh, o["agent"])
        except Exception as e:
            msg = str(e)[:300]
            _mark(op_id, status="error" if msg.startswith("МойСклад 4") else "queued", error=msg,
                  next_try=time.time() + min(3600, RETRY_EVERY * 2 ** min(att, 6)))
            print(f"Деньги: «{KINDS[o['kind']]}» {op_id[:8]} не проведён (попытка {att}): {msg}", flush=True)
    return get(op_id)


def _bg(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def refresh(agent_id):
    """Баланс контрагента и деньги на счетах — сразу из МойСклад (только чтение), чтобы не ждать прохода зеркала."""
    try:
        r = oh.ms("GET", f"/report/counterparty/{agent_id}", timeout=30)
        bal = (r.get("balance") or 0) / 100
        name = (r.get("counterparty") or {}).get("name") or ""
        with mirror.db() as d:
            if d.run("SELECT 1 FROM ms_agent_balance WHERE agent_id=%s", (agent_id,), one=True):
                d.run("UPDATE ms_agent_balance SET balance=%s, synced=%s WHERE agent_id=%s", (bal, time.time(), agent_id))
            else:
                d.run("INSERT INTO ms_agent_balance (agent_id, name, balance, synced) VALUES (%s, %s, %s, %s)", (agent_id, name, bal, time.time()))
        rows = mirror._report_rows("/report/money/byaccount")
        with mirror.db() as d:
            t = time.time()
            d.run("DELETE FROM ms_money")
            for x in rows:
                acc, org = x.get("account") or {}, x.get("organization") or {}
                aid = mirror._href_id(acc) or "cash:" + mirror._href_id(org)
                d.run("INSERT INTO ms_money (account_id, name, balance, synced) VALUES (%s, %s, %s, %s)",
                      (aid, acc.get("name") or x.get("name") or ("Касса" if not mirror._href_id(acc) else ""), (x.get("balance") or 0) / 100, t))
    except Exception as e:
        print("Деньги: не удалось сразу обновить баланс:", str(e)[:200], flush=True)


def retry(op_id):
    o = get(op_id)
    if o and o["status"] == "error":
        _mark(op_id, status="queued")
    return send(op_id)


def retry_pending():
    with db() as d:
        now = time.time()
        ids = [r[0] for r in d.run("SELECT id FROM pay_op WHERE status='queued' AND attempts<%s AND next_try<=%s AND created<%s ORDER BY created",
                                   (MAX_AUTO, now, now - 60), many=True) or []]
    for i in ids:
        send(i)
    return len(ids)


def agents(q, limit=20):
    """Контрагенты для платежа — из зеркала, с текущим балансом."""
    q = (q or "").strip()
    with mirror.db() as d:
        rows = d.run("SELECT a.id, a.name, a.phone, b.balance FROM ms_agent a LEFT JOIN ms_agent_balance b ON b.agent_id=a.id"
                     " WHERE a.deleted=0 AND a.archived=0 AND (LOWER(a.name) LIKE %s OR a.name LIKE %s OR a.phone LIKE %s) ORDER BY a.name LIMIT %s",
                     (f"%{q.lower()}%", f"%{q}%", f"%{q}%", limit), many=True) or []
    return [{"id": i, "name": n, "phone": p or "", "balance": round(b or 0)} for i, n, p, b in rows]


DOC_NAMES = {"customerorder": "Заказ", "demand": "Отгрузка", "retaildemand": "Чек", "salesreturn": "Возврат покупателя",
             "retailsalesreturn": "Возврат по чеку", "supply": "Приёмка", "paymentin": "Входящий платёж", "paymentout": "Исходящий платёж",
             "cashin": "Приходный ордер", "cashout": "Расходный ордер"}


def card(agent_id):
    """Карточка контрагента: баланс (знак как в отчёте взаиморасчётов: плюс — мы должны), документы за 90 дней, наши операции."""
    with mirror.db() as d:
        a = d.run("SELECT id, name, phone, email, inn FROM ms_agent WHERE id=%s AND deleted=0", (agent_id,), one=True)
        if not a:
            return None
        b = d.run("SELECT balance, synced FROM ms_agent_balance WHERE agent_id=%s", (agent_id,), one=True)
        docs = d.run("SELECT id, type, number, moment, sum, applicable FROM ms_doc WHERE agent_id=%s AND deleted=0 AND type IN ("
                     + ", ".join(["%s"] * len(DOC_NAMES)) + ") ORDER BY moment DESC LIMIT 60", (agent_id, *DOC_NAMES), many=True) or []
    bal = round((b or [0])[0] or 0)
    return {"id": a[0], "name": a[1] or "", "phone": a[2] or "", "email": a[3] or "", "inn": a[4] or "", "balance": bal,
            "synced": (b or [0, 0])[1] or 0,
            "docs": [{"id": i, "type": t, "title": DOC_NAMES.get(t, t), "number": n or "", "at": mirror._alm(m), "sum": round(s or 0),
                      "applicable": bool(ap), "money": t in mirror.MONEY_DOCS} for i, t, n, m, s, ap in docs],
            "ops": recent(20, agent_id)}


def debts(side, q="", limit=50, offset=0):
    """«Нам должны» (side=us, баланс < 0) или «Мы должны» (side=we, баланс > 0): полный список с поиском и итогом."""
    cond = "balance<0" if side == "us" else "balance>0"
    order = "ASC" if side == "us" else "DESC"
    q = (q or "").strip()
    with mirror.db() as d:
        where = f"{cond} AND (LOWER(name) LIKE %s OR name LIKE %s)"
        args = (f"%{q.lower()}%", f"%{q}%")
        tot = d.run(f"SELECT COUNT(*), COALESCE(SUM(balance), 0) FROM ms_agent_balance WHERE {where}", args, one=True)
        rows = d.run(f"SELECT agent_id, name, balance FROM ms_agent_balance WHERE {where} ORDER BY balance {order} LIMIT %s OFFSET %s",
                     (*args, limit, offset), many=True) or []
    return {"count": tot[0] or 0, "total": round(abs(tot[1] or 0)), "rows": [{"id": a, "name": n or "", "sum": round(abs(b or 0))} for a, n, b in rows]}


def _loop():
    time.sleep(140)
    while True:
        try:
            if enabled():
                retry_pending()
        except Exception as e:
            print("Деньги (очередь):", e, flush=True)
        time.sleep(RETRY_EVERY)


if os.environ.get("PORT"):
    threading.Thread(target=_loop, daemon=True).start()
