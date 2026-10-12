"""Склад: приёмка, списание, оприходование и перемещение из панели с двойной записью в МойСклад (этап 3 дорожной карты, docs/MIGRATION.md).

Как устроено:
- операция сначала сохраняется у нас (таблица wh_op, статус queued) — она не теряется, даже если МойСклад не ответил;
- сразу же отправляется в МойСклад (приёмка supply / списание loss), externalCode документа = id операции;
- при сбое сети или 5xx операция остаётся в очереди и повторяется в фоне; перед повтором документ ищется
  в МойСклад по externalCode — если запрос всё-таки прошёл, второй документ не создаётся;
- ответ 4xx (МойСклад отверг данные) не повторяется сам: статус error, владелец видит причину и нажимает «Повторить».
МойСклад остаётся главным: остатки меняет его документ, наше зеркало подтянет его при следующем проходе.
Флаг feat_wh_ops: по решению владельца 10.10.2026 включён, пока его не выключили вручную; работает только при включённом модуле «Склад»."""
import json
import os
import threading
import time
import uuid

import inbox
import mirror
import modules
import order_hook as oh

FLAG = "feat_wh_ops"
KINDS = {"supply": "Приёмка", "loss": "Списание", "enter": "Оприходование", "move": "Перемещение", "salesreturn": "Возврат покупателя",
         "purchasereturn": "Возврат поставщику"}
RETURNS = {"salesreturn": "demand", "purchasereturn": "supply"}   # возврат → документ, по которому он оформляется (колонка demand_id)
AGENT_KINDS = ("supply", "salesreturn", "purchasereturn")
PRICED = ("supply", "enter")   # цену вводит человек (закупка / себестоимость); у списания и перемещения — закупочная из карточки
RETRY_EVERY = 60          # с, фоновый повтор очереди
MAX_AUTO = 30             # после стольких неудачных попыток — только вручную
_ready = [False]
_lock = threading.Lock()  # одна отправка за раз: повтор из фона и нажатие «Повторить» не пересекаются


def _schema(d):
    if _ready[0]:
        return
    d.run("CREATE TABLE IF NOT EXISTS wh_op (id TEXT PRIMARY KEY, kind TEXT, store_id TEXT, agent_id TEXT, descr TEXT,"
          " lines TEXT, status TEXT, ms_id TEXT, ms_number TEXT, attempts INTEGER DEFAULT 0, error TEXT, who TEXT,"
          " created DOUBLE PRECISION, sent DOUBLE PRECISION, next_try DOUBLE PRECISION DEFAULT 0)")
    d.c.commit()
    for col in ("store2_id", "demand_id"):   # добавочные колонки: склад-получатель у перемещения, отгрузка у возврата
        try:
            d.run(f"ALTER TABLE wh_op ADD COLUMN {col} TEXT")
            d.c.commit()
        except Exception:
            d.c.rollback()
    _ready[0] = True


def db():
    d = inbox.db()
    _schema(d)
    return d


def enabled():
    if not modules.enabled("warehouse"):
        return False
    with inbox.db() as d:
        return inbox.get_setting(d, FLAG, "1") == "1"     # решение владельца 10.10.2026: включено сразу, выключатель — в «Товары → Склад»


def set_enabled(on):
    with inbox.db() as d:
        inbox.set_setting(d, FLAG, "1" if on else "0")


def _row(r):
    i, kind, store, agent, descr, lines, status, ms_id, ms_num, att, err, who, created, sent, store2, demand = r[:16]
    return {"id": i, "kind": kind, "title": KINDS.get(kind, kind), "store": store, "store2": store2 or "", "agent": agent, "descr": descr or "",
            "lines": json.loads(lines or "[]"), "status": status, "msId": ms_id or "", "msNumber": ms_num or "",
            "attempts": att or 0, "error": err or "", "who": who or "", "created": created or 0, "sent": sent or 0, "demand": demand or ""}


_OCOLS = "id, kind, store_id, agent_id, descr, lines, status, ms_id, ms_number, attempts, error, who, created, sent, store2_id, demand_id"


def get(op_id):
    with db() as d:
        r = d.run(f"SELECT {_OCOLS} FROM wh_op WHERE id=%s", (op_id,), one=True)
    return _row(r) if r else None


def recent(limit=30):
    with db() as d:
        rows = d.run(f"SELECT {_OCOLS} FROM wh_op ORDER BY created DESC LIMIT %s", (limit,), many=True) or []
        names = dict(d.run("SELECT id, name FROM ms_store", many=True) or [])
        agents = {}
        ids = list({r[3] for r in rows if r[3]})
        if ids:
            agents = dict(d.run("SELECT id, name FROM ms_agent WHERE id IN (" + ", ".join(["%s"] * len(ids)) + ")", ids, many=True) or [])
    out = []
    for r in rows:
        o = _row(r)
        o["storeName"], o["agentName"], o["store2Name"] = names.get(o["store"], ""), agents.get(o["agent"], ""), names.get(o["store2"], "")
        o["sum"] = round(sum(x["qty"] * x["price"] for x in o["lines"]))
        out.append(o)
    return out


def demand_lines(demand_id, kind="salesreturn"):
    """Документ для возврата (отгрузка — возврат покупателя, приёмка — возврат поставщику): контрагент, склад и позиции —
    было в документе, уже возвращено из системы, можно вернуть; цена со скидкой."""
    base = RETURNS[kind]
    with mirror.db() as d:
        x = d.run("SELECT id, number, agent_id, store_id, applicable FROM ms_doc WHERE id=%s AND type=%s AND deleted=0", (demand_id, base), one=True)
        if not x:
            return None
        rows = d.run("SELECT l.product_id, p.name, p.code, p.kind, SUM(l.qty), SUM(l.sum) FROM ms_doc_line l LEFT JOIN ms_product p ON p.id=l.product_id"
                     " WHERE l.doc_id=%s GROUP BY l.product_id, p.name, p.code, p.kind ORDER BY p.name", (demand_id,), many=True) or []
    back = {}
    with db() as d:
        for (lines,) in d.run("SELECT lines FROM wh_op WHERE kind=%s AND demand_id=%s AND status<>'error'", (kind, demand_id), many=True) or []:
            for l in json.loads(lines or "[]"):
                back[l["id"]] = back.get(l["id"], 0) + l["qty"]
    out = [{"id": pid, "name": n or "(товар удалён)", "code": c or "", "kind": k or "product", "qty": q or 0, "returned": back.get(pid, 0),
            "left": round((q or 0) - back.get(pid, 0), 3), "price": round((sm or 0) / q) if q else 0} for pid, n, c, k, q, sm in rows]
    return {"id": x[0], "number": x[1] or "", "agent": x[2] or "", "store": x[3] or "", "applicable": bool(x[4]), "lines": out}


def create_return(demand_id, lines, descr="", who="", kind="salesreturn"):
    """Возврат по документу: покупателя — по отгрузке, поставщику — по приёмке. Склад и контрагент — из документа,
    цены — как в нём, не больше, чем было в документе (за вычетом прежних возвратов из системы)."""
    if kind not in RETURNS:
        raise ValueError("Неизвестный возврат")
    what = "Отгрузка" if kind == "salesreturn" else "Приёмка"
    dm = demand_lines(demand_id, kind)
    if not dm or not dm["applicable"]:
        raise ValueError(f"{what} не найдена или не проведена")
    if not dm["agent"] or not dm["store"]:
        raise ValueError(f"У документа нет контрагента или склада")
    have = {l["id"]: l for l in dm["lines"]}
    clean = []
    for x in lines or []:
        pid = str(x.get("id") or "")
        try:
            q = float(x.get("qty") or 0)
        except (TypeError, ValueError):
            raise ValueError("Количество — число")
        if q <= 0:
            continue
        if pid not in have:
            raise ValueError("Этого товара нет в документе")
        if q > have[pid]["left"] + 1e-9:
            raise ValueError(f"«{have[pid]['name'][:40]}»: вернуть можно не больше {have[pid]['left']:g} шт.")
        clean.append({"id": pid, "qty": q, "price": have[pid]["price"]})
    if not clean:
        raise ValueError("Укажите, сколько возвращают")
    return create(kind, dm["store"], clean, dm["agent"], descr, who, demand_id=demand_id)


def create(kind, store_id, lines, agent_id="", descr="", who="", store2_id="", op_id="", demand_id=""):
    """Проверяет и сохраняет операцию, затем сразу пробует отправить в МойСклад. Ошибка проверки — ValueError.
    op_id — свой id операции (инвентаризация): если такая операция уже есть, вторая не создаётся."""
    if op_id and get(op_id):
        return send(op_id)
    if kind not in KINDS:
        raise ValueError("Неизвестная операция")
    if kind in RETURNS and not demand_id:
        raise ValueError("Возврат оформляется из документа (отгрузки или приёмки)")
    descr = (descr or "").strip()[:500]
    with db() as d:
        if not d.run("SELECT 1 FROM ms_store WHERE id=%s AND archived=0", (store_id,), one=True):
            raise ValueError("Выберите склад")
        if kind in AGENT_KINDS and not d.run("SELECT 1 FROM ms_agent WHERE id=%s AND deleted=0", (agent_id or "",), one=True):
            raise ValueError("Выберите поставщика")
        if kind == "move":
            if not store2_id or store2_id == store_id or not d.run("SELECT 1 FROM ms_store WHERE id=%s AND archived=0", (store2_id,), one=True):
                raise ValueError("Выберите склад, куда перемещаете (другой)")
        else:
            store2_id = ""
        clean = []
        for x in lines or []:
            pid = str(x.get("id") or "")
            try:
                qty, price = float(x.get("qty") or 0), float(x.get("price") or 0)
            except (TypeError, ValueError):
                raise ValueError("Количество и цена — числа")
            if qty <= 0 or price < 0:
                raise ValueError("Количество должно быть больше нуля")
            p = d.run("SELECT kind, name, buy_price FROM ms_product WHERE id=%s AND deleted=0", (pid,), one=True)
            if not p:
                raise ValueError("Товар не найден — обновите список")
            clean.append({"id": pid, "kind": p[0] or "product", "name": p[1], "qty": qty,
                          "price": round(price if kind in PRICED + tuple(RETURNS) else (p[2] or 0))})
        if not clean:
            raise ValueError("Добавьте хотя бы один товар")
        op_id = op_id or str(uuid.uuid4())
        d.run(f"INSERT INTO wh_op ({_OCOLS}, next_try) VALUES (%s, %s, %s, %s, %s, %s, 'queued', '', '', 0, '', %s, %s, 0, %s, %s, 0)",
              (op_id, kind, store_id, agent_id if kind in AGENT_KINDS else "", descr, json.dumps(clean, ensure_ascii=False), who, time.time(),
               store2_id, demand_id if kind in RETURNS else ""))
    send(op_id)
    return get(op_id)


def _body(o):
    pos = [{"quantity": x["qty"], "price": round(x["price"] * 100), "assortment": oh.meta(x.get("kind") or "product", x["id"])}
           for x in o["lines"]]
    body = {"externalCode": o["id"], "organization": oh.meta("organization", oh.organization()),
            "positions": pos, "applicable": True,
            "description": ("Из панели AMURA" + (f" ({o['who']})" if o["who"] else "") + (": " + o["descr"] if o["descr"] else ""))[:4000]}
    if o["kind"] == "move":            # перемещение: склад-отправитель и склад-получатель
        body["sourceStore"], body["targetStore"] = oh.meta("store", o["store"]), oh.meta("store", o["store2"])
    else:
        body["store"] = oh.meta("store", o["store"])
    if o["kind"] in AGENT_KINDS:
        body["agent"] = oh.meta("counterparty", o["agent"])
    if o["kind"] in RETURNS:                    # ссылка на документ: demand у возврата покупателя, supply у возврата поставщику
        body[RETURNS[o["kind"]]] = oh.meta(RETURNS[o["kind"]], o["demand"])
    return body


def _mark(op_id, **f):
    sets = ", ".join(f"{k}=%s" for k in f)
    with db() as d:
        d.run(f"UPDATE wh_op SET {sets} WHERE id=%s", (*f.values(), op_id))


def send(op_id):
    """Одна попытка отправить операцию. Возвращает её состояние. Никогда не создаёт второй документ в МойСклад."""
    with _lock:
        o = get(op_id)
        if not o or o["status"] == "sent":
            return o
        att = o["attempts"] + 1
        _mark(op_id, attempts=att)       # до запроса: если процесс упадёт посреди записи, повтор сначала поищет документ
        try:
            if o["attempts"]:      # прошлая попытка могла дойти: сначала ищем документ по externalCode
                found = oh.ms("GET", f"/entity/{o['kind']}", params={"filter": f"externalCode={o['id']}", "limit": 1}, timeout=20).get("rows") or []
                if found:
                    _mark(op_id, status="sent", ms_id=found[0]["id"], ms_number=found[0].get("name") or "", attempts=att, error="", sent=time.time())
                    print(f"Склад: операция «{KINDS[o['kind']]}» {op_id[:8]} уже есть в МойСклад (№{found[0].get('name')})", flush=True)
                    return get(op_id)
            r = oh.ms("POST", f"/entity/{o['kind']}", json=_body(o), timeout=30)
            _mark(op_id, status="sent", ms_id=r.get("id") or "", ms_number=r.get("name") or "", attempts=att, error="", sent=time.time())
            print(f"Склад: операция «{KINDS[o['kind']]}» {op_id[:8]} проведена в МойСклад №{r.get('name')}", flush=True)
        except Exception as e:
            msg = str(e)[:300]
            rejected = msg.startswith("МойСклад 4")      # данные отвергнуты — сам не повторяем
            _mark(op_id, status="error" if rejected else "queued", attempts=att, error=msg,
                  next_try=time.time() + min(3600, RETRY_EVERY * 2 ** min(att, 6)))
            print(f"Склад: операция «{KINDS[o['kind']]}» {op_id[:8]} не отправлена (попытка {att}): {msg}", flush=True)
        return get(op_id)


def retry(op_id):
    """«Повторить» из панели: в том числе после отказа МойСклад (например, поправили карточку товара)."""
    o = get(op_id)
    if o and o["status"] == "error":
        _mark(op_id, status="queued")
    return send(op_id)


def retry_pending():
    with db() as d:
        now = time.time()           # created < now−60: только что созданную операцию отправляет сам create()
        ids = [r[0] for r in d.run("SELECT id FROM wh_op WHERE status='queued' AND attempts<%s AND next_try<=%s AND created<%s ORDER BY created",
                                   (MAX_AUTO, now, now - 60), many=True) or []]
    for i in ids:
        send(i)
    return len(ids)


def _loop():
    time.sleep(120)
    while True:
        try:
            if enabled():
                retry_pending()
        except Exception as e:
            print("Склад (очередь):", e, flush=True)
        time.sleep(RETRY_EVERY)


def agents(q, limit=20):
    """Поставщики для приёмки — из зеркала контрагентов."""
    q = (q or "").strip()
    with mirror.db() as d:
        rows = d.run("SELECT id, name, phone FROM ms_agent WHERE deleted=0 AND archived=0 AND (LOWER(name) LIKE %s OR name LIKE %s OR phone LIKE %s)"
                     " ORDER BY name LIMIT %s", (f"%{q.lower()}%", f"%{q}%", f"%{q}%", limit), many=True) or []
    return [{"id": i, "name": n, "phone": p or ""} for i, n, p in rows]


if os.environ.get("PORT"):          # только на сервере; пока флаг выключен, очередь не трогается
    threading.Thread(target=_loop, daemon=True).start()
