"""Отгрузка из заказа прямо в системе AMURA (без сценария МойСклад) — двойная запись, как у склада и денег.

- по умолчанию выключено (флаг feat_ship_ops): сценарий МойСклад, создающий отгрузку по статусу, продолжает работать как раньше;
- перед созданием проверяем заказ: если по нему уже есть отгрузка (например, её создал сценарий) — вторую не создаём;
- отгрузка строится шаблоном МойСклад по заказу (PUT /entity/demand/new с customerOrder): позиции, цены, скидки,
  склад, контрагент — ровно как в заказе; externalCode = id операции, повтор сначала ищет документ по нему;
- статус заказа не меняем: смена статуса может запустить сценарий и создать вторую отгрузку."""
import os
import threading
import time
import uuid

import inbox
import order_hook as oh

FLAG = "feat_ship_ops"
RETRY_EVERY = 60
MAX_AUTO = 30
_ready = [False]
_lock = threading.Lock()


def _schema(d):
    if _ready[0]:
        return
    d.run("CREATE TABLE IF NOT EXISTS ship_op (id TEXT PRIMARY KEY, order_id TEXT, order_number TEXT, status TEXT, ms_id TEXT, ms_number TEXT,"
          " attempts INTEGER DEFAULT 0, error TEXT, who TEXT, created DOUBLE PRECISION, sent DOUBLE PRECISION, next_try DOUBLE PRECISION DEFAULT 0)")
    d.c.commit()
    _ready[0] = True


def db():
    d = inbox.db()
    _schema(d)
    return d


def enabled():
    with inbox.db() as d:
        return inbox.get_setting(d, FLAG, "0") == "1"     # выключено, пока владелец не включит: рядом работает сценарий МойСклад


def set_enabled(on):
    with inbox.db() as d:
        inbox.set_setting(d, FLAG, "1" if on else "0")


_COLS = "id, order_id, order_number, status, ms_id, ms_number, attempts, error, who, created, sent"


def _row(r):
    i, oid, num, st, ms_id, ms_num, att, err, who, created, sent = r
    return {"id": i, "order": oid, "number": num or "", "status": st, "msId": ms_id or "", "msNumber": ms_num or "", "attempts": att or 0,
            "error": err or "", "who": who or "", "created": created or 0, "sent": sent or 0}


def get(op_id):
    with db() as d:
        r = d.run(f"SELECT {_COLS} FROM ship_op WHERE id=%s", (op_id,), one=True)
    return _row(r) if r else None


def for_order(order_id):
    with db() as d:
        r = d.run(f"SELECT {_COLS} FROM ship_op WHERE order_id=%s AND status<>'error' ORDER BY created DESC LIMIT 1", (order_id,), one=True)
    return _row(r) if r else None


def _demands(order_id):
    o = oh.ms("GET", f"/entity/customerorder/{order_id}", timeout=30)
    return o, [(x.get("meta") or {}).get("href", "").rstrip("/").rsplit("/", 1)[-1] for x in o.get("demands") or []]


def create(order_id, who=""):
    """Отгрузить заказ. Уже есть отгрузка по заказу (наша или сценария) — ValueError, второй не будет."""
    with _lock:
        mine = for_order(order_id)
        if mine:
            if mine["status"] == "sent":
                raise ValueError(f"Заказ уже отгружен: отгрузка №{mine['msNumber']}")
            return mine                                   # в очереди — дождёмся её, новую не создаём
        o, dem = _demands(order_id)
        if dem:
            raise ValueError("По заказу уже есть отгрузка в МойСклад")
        op_id = str(uuid.uuid4())
        with db() as d:
            d.run(f"INSERT INTO ship_op ({_COLS}, next_try) VALUES (%s, %s, %s, 'queued', '', '', 0, '', %s, %s, 0, 0)",
                  (op_id, order_id, o.get("name") or "", who, time.time()))
    return send(op_id)


def _mark(op_id, **f):
    sets = ", ".join(f"{k}=%s" for k in f)
    with db() as d:
        d.run(f"UPDATE ship_op SET {sets} WHERE id=%s", (*f.values(), op_id))


def send(op_id):
    with _lock:
        o = get(op_id)
        if not o or o["status"] == "sent":
            return o
        att = o["attempts"] + 1
        _mark(op_id, attempts=att)
        try:
            row = None
            if o["attempts"]:                             # прошлая попытка могла дойти
                found = oh.ms("GET", "/entity/demand", params={"filter": f"externalCode={op_id}", "limit": 1}, timeout=20).get("rows") or []
                row = found[0] if found else None
            if row is None:
                _, dem = _demands(o["order"])
                if dem:                                   # пока ждали — отгрузку создал сценарий или человек в МойСклад
                    _mark(op_id, status="error", error="По заказу уже есть отгрузка в МойСклад — вторую не создаю")
                    return get(op_id)
                tpl = oh.ms("PUT", "/entity/demand/new", json={"customerOrder": oh.meta("customerorder", o["order"])}, timeout=30)
                tpl.update({"externalCode": op_id, "applicable": True,
                            "description": ((tpl.get("description") or "") + "\nОтгружено из системы AMURA" + (f" ({o['who']})" if o["who"] else "")).strip()[:4000]})
                row = oh.ms("POST", "/entity/demand", json=tpl, timeout=30)
            _mark(op_id, status="sent", ms_id=row.get("id") or "", ms_number=row.get("name") or "", error="", sent=time.time())
            print(f"Отгрузка: заказ №{o['number']} отгружен из системы, отгрузка №{row.get('name')}", flush=True)
        except Exception as e:
            msg = str(e)[:300]
            _mark(op_id, status="error" if msg.startswith("МойСклад 4") else "queued", error=msg,
                  next_try=time.time() + min(3600, RETRY_EVERY * 2 ** min(att, 6)))
            print(f"Отгрузка: заказ №{o['number']} не отгружен (попытка {att}): {msg}", flush=True)
    return get(op_id)


def retry_pending():
    with db() as d:
        now = time.time()
        ids = [r[0] for r in d.run("SELECT id FROM ship_op WHERE status='queued' AND attempts<%s AND next_try<=%s AND created<%s ORDER BY created",
                                   (MAX_AUTO, now, now - 60), many=True) or []]
    for i in ids:
        send(i)
    return len(ids)


def _loop():
    time.sleep(160)
    while True:
        try:
            if enabled():
                retry_pending()
        except Exception as e:
            print("Отгрузки (очередь):", e, flush=True)
        time.sleep(RETRY_EVERY)


if os.environ.get("PORT"):
    threading.Thread(target=_loop, daemon=True).start()
