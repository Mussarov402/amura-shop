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
ORG = "org:"                 # контрагент-организация: перемещение денег между своими счётом и кассой
TRANSFER_ITEM = "Перемещение"   # статья расходов для перемещения (есть в МойСклад)
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
    for col in ("doc_id", "doc_type"):        # добавочные колонки: оплата по конкретной отгрузке / приёмке
        try:
            d.run(f"ALTER TABLE pay_op ADD COLUMN {col} TEXT")
            d.c.commit()
        except Exception:
            d.c.rollback()
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
    i, kind, agent, s, acc, exp, moment, descr, status, ms_id, ms_num, att, err, who, created, sent, doc_id, doc_type = r[:18]
    return {"id": i, "kind": kind, "title": KINDS.get(kind, kind), "agent": agent, "sum": s or 0, "account": acc or "", "expense": exp or "",
            "moment": moment or "", "descr": descr or "", "status": status, "msId": ms_id or "", "msNumber": ms_num or "",
            "attempts": att or 0, "error": err or "", "who": who or "", "created": created or 0, "sent": sent or 0,
            "docId": doc_id or "", "docType": doc_type or ""}


_COLS = "id, kind, agent_id, sum, account, expense, moment, descr, status, ms_id, ms_number, attempts, error, who, created, sent, doc_id, doc_type"


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
        o["agentName"] = "Перемещение денег" if o["agent"].startswith(ORG) else names.get(o["agent"], "")
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


LINK = {"in": "demand", "out": "supply"}    # оплата — по отгрузке покупателю, выплата — по приёмке от поставщика


def doc_due(doc_id):
    """Документ и долг по нему: сумма минус оплачено (большее из payedSum МойСклад и связанных платежей), ₸."""
    with mirror.db() as d:
        r = d.run("SELECT x.type, x.number, x.agent_id, x.sum, COALESCE(x.paid, 0), COALESCE(l.s, 0), x.applicable FROM ms_doc x"
                  " LEFT JOIN (SELECT doc_id, SUM(sum) AS s FROM ms_payment_link GROUP BY doc_id) l ON l.doc_id=x.id"
                  " WHERE x.id=%s AND x.deleted=0", (doc_id,), one=True)
    if not r:
        return None
    t, num, agent, s, paid, linked, ap = r
    return {"id": doc_id, "type": t, "number": num or "", "agent": agent or "", "sum": round(s or 0),
            "paid": round(max(paid, linked)), "due": round((s or 0) - max(paid, linked)), "applicable": bool(ap)}


def create(direction, agent_id, amount, account="", expense="", day="", descr="", who="", doc_id=""):
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
    doc_type = ""
    if doc_id:
        doc = doc_due(doc_id)
        if not doc or direction not in LINK or doc["type"] != LINK[direction]:
            raise ValueError("Документ для оплаты не найден")
        if doc["agent"] != agent_id:
            raise ValueError("Документ другого контрагента")
        if s > doc["due"] + 0.5:
            raise ValueError(f"Сумма больше долга по документу ({doc['due']:,.0f} ₸)".replace(",", " ") + " — уменьшите или проведите без привязки")
        doc_type = doc["type"]
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
        d.run(f"INSERT INTO pay_op ({_COLS}, next_try) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', '', '', 0, '', %s, %s, 0, %s, %s, 0)",
              (op_id, kind, agent_id, s, account, expense, moment, descr, who, time.time(), doc_id or "", doc_type))
    send(op_id)
    return get(op_id)


def transfer(src, dst, amount, day="", descr="", who=""):
    """Перемещение денег между своими счётом и кассой (как в МойСклад): расход из источника (исходящий платёж / расходный ордер,
    статья «Перемещение») и приход в получателя (входящий платёж / приходный ордер), контрагент — своя организация.
    Это две операции очереди: каждая со своим externalCode, повтор без дублей."""
    try:
        s = round(float(amount), 2)
    except (TypeError, ValueError):
        raise ValueError("Сумма — число")
    if s <= 0 or s > MAX_SUM:
        raise ValueError("Сумма должна быть больше нуля" if s <= 0 else "Слишком большая сумма — проверьте нули")
    m = meta()
    accs = {a["id"] for a in m["accounts"]}
    if src not in accs or dst not in accs:
        raise ValueError("Выберите, откуда и куда")
    if src == dst:
        raise ValueError("Откуда и куда — разные счета")
    item = next((x["id"] for x in m["expenses"] if x["name"].strip().lower() == TRANSFER_ITEM.lower()), "")
    if not item:
        raise ValueError(f"В МойСклад нет статьи расходов «{TRANSFER_ITEM}»")
    moment, agent = _moment(day), ORG + oh.organization()
    descr = ((descr or "").strip() or "Перемещение денег")[:500]
    ids = []
    with db() as d:
        for kind, acc, exp in ((("cashout" if src == CASH else "paymentout"), src, item), (("cashin" if dst == CASH else "paymentin"), dst, "")):
            op_id = str(uuid.uuid4())
            d.run(f"INSERT INTO pay_op ({_COLS}, next_try) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued', '', '', 0, '', %s, %s, 0, '', '', 0)",
                  (op_id, kind, agent, s, acc, exp, moment, descr, who, time.time()))
            ids.append(op_id)
    return [send(i) for i in ids]


def _body(o):
    org = oh.organization()
    agent = oh.meta("organization", o["agent"][len(ORG):]) if o["agent"].startswith(ORG) else oh.meta("counterparty", o["agent"])
    body = {"externalCode": o["id"], "organization": oh.meta("organization", org), "agent": agent,
            "moment": o["moment"], "applicable": True, "sum": round(o["sum"] * 100),
            "description": ("Из системы AMURA" + (f" ({o['who']})" if o["who"] else "") + (": " + o["descr"] if o["descr"] else ""))[:4000]}
    if o["kind"] != "counterpartyadjustment":
        body["paymentPurpose"] = o["descr"][:255]
    if o["kind"] in ("paymentin", "paymentout") and o["account"] and o["account"] != CASH:
        body["organizationAccount"] = {"meta": {"href": f"{oh.API}/entity/organization/{org}/accounts/{o['account']}",
                                                "type": "account", "mediaType": "application/json"}}
    if o["kind"] in ("paymentout", "cashout"):
        body["expenseItem"] = oh.meta("expenseitem", o["expense"])
    if o["docId"]:
        body["operations"] = [{**oh.meta(o["docType"], o["docId"]), "linkedSum": round(o["sum"] * 100)}]
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
            if o["docId"] and row.get("id"):     # документ сразу виден оплаченным (зеркало потом перезапишет то же)
                with mirror.db() as d:
                    d.run("INSERT INTO ms_payment_link (payment_id, doc_id, doc_type, sum) VALUES (%s, %s, %s, %s) ON CONFLICT (payment_id, doc_id) DO NOTHING",
                          (row["id"], o["docId"], o["docType"], o["sum"]))
                    d.run("UPDATE ms_doc SET paid=COALESCE(paid, 0)+%s WHERE id=%s", (o["sum"], o["docId"]))   # payedSum до следующего прохода
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
        if agent_id.startswith(ORG):
            raise LookupError                    # перемещение денег: контрагента нет, только деньги на счетах
        r = oh.ms("GET", f"/report/counterparty/{agent_id}", timeout=30)
        bal = (r.get("balance") or 0) / 100
        name = (r.get("counterparty") or {}).get("name") or ""
        with mirror.db() as d:
            if d.run("SELECT 1 FROM ms_agent_balance WHERE agent_id=%s", (agent_id,), one=True):
                d.run("UPDATE ms_agent_balance SET balance=%s, synced=%s WHERE agent_id=%s", (bal, time.time(), agent_id))
            else:
                d.run("INSERT INTO ms_agent_balance (agent_id, name, balance, synced) VALUES (%s, %s, %s, %s)", (agent_id, name, bal, time.time()))
    except LookupError:
        pass
    except Exception as e:
        print("Деньги: не удалось сразу обновить баланс контрагента:", str(e)[:200], flush=True)
    try:
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
        print("Деньги: не удалось сразу обновить деньги на счетах:", str(e)[:200], flush=True)


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


DOC_GROUPS = {"sale": ("customerorder", "demand", "retaildemand", "salesreturn", "retailsalesreturn"), "supply": ("supply",),
              "money": ("paymentin", "paymentout", "cashin", "cashout")}


def card(agent_id, q="", group="", limit=60):
    """Карточка контрагента: баланс (знак как в отчёте взаиморасчётов: плюс — мы должны), документы за 90 дней, наши операции.
    q — поиск документа по номеру (часть номера), group — sale / supply / money (остальное — все виды)."""
    types = DOC_GROUPS.get(group) or tuple(DOC_NAMES)
    q = "".join(ch for ch in str(q or "") if ch.isalnum())[:30]
    with mirror.db() as d:
        a = d.run("SELECT id, name, phone, email, inn FROM ms_agent WHERE id=%s AND deleted=0", (agent_id,), one=True)
        if not a:
            return None
        b = d.run("SELECT balance, synced FROM ms_agent_balance WHERE agent_id=%s", (agent_id,), one=True)
        where = "agent_id=%s AND deleted=0 AND type IN (" + ", ".join(["%s"] * len(types)) + ")" + (" AND number LIKE %s" if q else "")
        args = (agent_id, *types, *((f"%{q}%",) if q else ()))
        total = (d.run(f"SELECT COUNT(*) FROM ms_doc WHERE {where}", args, one=True) or [0])[0] or 0
        docs = d.run(f"SELECT id, type, number, moment, sum, applicable FROM ms_doc WHERE {where} ORDER BY moment DESC LIMIT %s",
                     (*args, limit), many=True) or []
    bal = round((b or [0])[0] or 0)
    return {"id": a[0], "name": a[1] or "", "phone": a[2] or "", "email": a[3] or "", "inn": a[4] or "", "balance": bal,
            "synced": (b or [0, 0])[1] or 0, "docsTotal": total,
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
