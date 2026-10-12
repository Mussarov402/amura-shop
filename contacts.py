"""Контрагенты: создание и правка карточки клиента / поставщика из системы AMURA с двойной записью в МойСклад.

- операция сначала сохраняется у нас (agent_q), затем отправляется: новый — POST /entity/counterparty с externalCode = id
  операции (повтор сначала ищет по нему — второй карточки не будет), правка — GET текущей карточки и PUT только изменённых
  полей (PUT идемпотентен);
- поля: название, телефон, город (фактический адрес), теги, тип цены (только владелец — от него зависят цены клиента);
- новый клиент с телефоном, который уже есть у другого контрагента, не создаётся (подсказка — открыть существующего);
- после записи наша копия (ms_agent) обновляется сразу; зеркало потом перезапишет то же.
МойСклад остаётся главным."""
import json
import os
import re
import threading
import time
import uuid

import inbox
import mirror
import order_hook as oh

RETRY_EVERY = 60
MAX_AUTO = 30
_ready = [False]
_lock = threading.Lock()
_meta = {}


def _schema(d):
    if _ready[0]:
        return
    d.run("CREATE TABLE IF NOT EXISTS agent_q (id TEXT PRIMARY KEY, kind TEXT, agent_id TEXT, data TEXT, status TEXT, attempts INTEGER DEFAULT 0,"
          " error TEXT, who TEXT, created DOUBLE PRECISION, sent DOUBLE PRECISION, next_try DOUBLE PRECISION DEFAULT 0)")
    d.c.commit()
    _ready[0] = True


def db():
    d = inbox.db()
    _schema(d)
    return d


def price_types():
    """Типы цен МойСклад (кэш 10 минут) — для поля «Тип цены» у клиента."""
    c = _meta.get("pt")
    if not c or time.time() - c[0] > 600:
        pts = oh.ms("GET", "/context/companysettings/pricetype", timeout=30)
        pts = pts if isinstance(pts, list) else pts.get("rows", [])
        c = (time.time(), [{"id": p.get("id") or "", "name": p.get("name") or ""} for p in pts])
        _meta["pt"] = c
    return c[1]


def _digits(p):
    return re.sub(r"\D", "", p or "")[-10:]


def _clean(data, owner):
    """Проверка полей. Возвращает только переданные поля в виде для МойСклад-карточки."""
    out = {}
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name or len(name) > 255:
            raise ValueError("Название — от 1 до 255 символов")
        out["name"] = name
    if "phone" in data:
        ph = str(data.get("phone") or "").strip()
        if ph and len(re.sub(r"\D", "", ph)) < 10:
            raise ValueError("Телефон — не меньше 10 цифр")
        out["phone"] = ("+" + oh.norm_phone(ph)) if ph else ""
    if "city" in data:
        out["actualAddress"] = str(data.get("city") or "").strip()[:255]
    if "tags" in data:
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = tags.split(",")
        out["tags"] = sorted({str(t).strip().lower()[:50] for t in tags if str(t).strip()})
    if "priceType" in data:
        if not owner:
            raise PermissionError("Тип цены меняет только владелец")
        pt = str(data.get("priceType") or "")
        if pt and pt not in {p["id"] for p in price_types()}:
            raise ValueError("Нет такого типа цены")
        out["priceType"] = pt
    return out


def _same_phone(phone, exclude=""):
    d10 = _digits(phone)
    if len(d10) < 10:
        return None
    with mirror.db() as d:
        for i, n, p in d.run("SELECT id, name, phone FROM ms_agent WHERE deleted=0 AND phone<>''", many=True) or []:   # телефоны в любом формате — сравниваем цифры
            if i != exclude and _digits(p) == d10:
                return {"id": i, "name": n}
    return None


def create(data, who="", owner=False):
    x = _clean(data, owner)
    if not x.get("name"):
        raise ValueError("Укажите название")
    dup = _same_phone(x.get("phone"))
    if dup:
        raise ValueError(f"Телефон уже у контрагента «{dup['name']}» — откройте его карточку")
    return _enqueue("create", "", x, who)


def edit(agent_id, data, who="", owner=False):
    x = _clean(data, owner)
    if not x:
        raise ValueError("Нечего менять")
    if x.get("phone"):
        dup = _same_phone(x["phone"], exclude=agent_id)
        if dup:
            raise ValueError(f"Телефон уже у контрагента «{dup['name']}»")
    with mirror.db() as d:
        if not d.run("SELECT 1 FROM ms_agent WHERE id=%s AND deleted=0", (agent_id,), one=True):
            raise LookupError("Контрагент не найден")
    return _enqueue("edit", agent_id, x, who)


def _enqueue(kind, agent_id, x, who):
    op = str(uuid.uuid4())
    with db() as d:
        d.run("INSERT INTO agent_q (id, kind, agent_id, data, status, attempts, error, who, created, sent, next_try)"
              " VALUES (%s, %s, %s, %s, 'queued', 0, '', %s, %s, 0, 0)", (op, kind, agent_id, json.dumps(x, ensure_ascii=False), who, time.time()))
    return send(op)


def get(op):
    with db() as d:
        r = d.run("SELECT id, kind, agent_id, data, status, attempts, error, who, created FROM agent_q WHERE id=%s", (op,), one=True)
    if not r:
        return None
    return {"id": r[0], "kind": r[1], "agent": r[2] or "", "data": json.loads(r[3] or "{}"), "status": r[4], "attempts": r[5] or 0,
            "error": r[6] or "", "who": r[7] or "", "created": r[8] or 0}


def _mark(op, **f):
    sets = ", ".join(f"{k}=%s" for k in f)
    with db() as d:
        d.run(f"UPDATE agent_q SET {sets} WHERE id=%s", (*f.values(), op))


def _ms_fields(x):
    body = {k: v for k, v in x.items() if k != "priceType"}
    if "priceType" in x:
        body["priceType"] = ({"meta": {"href": f"{oh.API}/context/companysettings/pricetype/{x['priceType']}", "type": "pricetype",
                                       "mediaType": "application/json"}} if x["priceType"] else None)
    return body


def send(op):
    with _lock:
        o = get(op)
        if not o or o["status"] == "sent":
            return o
        att = o["attempts"] + 1
        _mark(op, attempts=att)
        x = o["data"]
        try:
            if o["kind"] == "create":
                row = None
                if o["attempts"]:
                    found = oh.ms("GET", "/entity/counterparty", params={"filter": f"externalCode={op}", "limit": 1}, timeout=20).get("rows") or []
                    row = found[0] if found else None
                if row is None:
                    row = oh.ms("POST", "/entity/counterparty", json={"externalCode": op, **_ms_fields(x)}, timeout=30)
                aid = row.get("id") or ""
                _mark(op, status="sent", agent_id=aid, error="", sent=time.time())
            else:
                aid = o["agent"]
                cur = oh.ms("GET", f"/entity/counterparty/{aid}", timeout=20)
                cur_pt = ((cur.get("priceType") or {}).get("meta") or {}).get("href", "").rstrip("/").rsplit("/", 1)[-1] if cur.get("priceType") else ""
                diff = {k: v for k, v in x.items() if (cur_pt if k == "priceType" else (sorted(cur.get(k) or []) if k == "tags" else cur.get(k) or "")) != v}
                if diff:
                    row = oh.ms("PUT", f"/entity/counterparty/{aid}", json=_ms_fields(diff), timeout=30)
                else:
                    row = cur
                _mark(op, status="sent", error="", sent=time.time())
            _local(aid, row, x)
            print(f"Контрагенты: {'создан' if o['kind'] == 'create' else 'изменён'} «{(row.get('name') or x.get('name') or '')[:40]}» ({aid[:8]})", flush=True)
        except Exception as e:
            msg = str(e)[:300]
            _mark(op, status="error" if msg.startswith("МойСклад 4") else "queued", error=msg,
                  next_try=time.time() + min(3600, RETRY_EVERY * 2 ** min(att, 6)))
            print(f"Контрагенты: операция {op[:8]} не отправлена (попытка {att}): {msg}", flush=True)
    return get(op)


def _local(aid, row, x):
    """Наша копия сразу видит изменения (зеркало потом перезапишет то же)."""
    if not aid:
        return
    name, phone = row.get("name") or x.get("name") or "", row.get("phone") if "phone" in row else x.get("phone")
    tags = row.get("tags") if "tags" in row else x.get("tags")
    with mirror.db() as d:
        if d.run("SELECT 1 FROM ms_agent WHERE id=%s", (aid,), one=True):
            sets, args = ["name=%s"], [name]
            if phone is not None:
                sets.append("phone=%s"), args.append(phone or "")
            if tags is not None:
                sets.append("tags=%s"), args.append(json.dumps(tags, ensure_ascii=False))
            d.run(f"UPDATE ms_agent SET {', '.join(sets)} WHERE id=%s", (*args, aid))
        else:
            d.run("INSERT INTO ms_agent (id, name, phone, email, inn, company_type, tags, archived, updated, deleted, seen)"
                  " VALUES (%s, %s, %s, '', '', %s, %s, 0, %s, 0, %s)",
                  (aid, name, phone or "", row.get("companyType") or "legal", json.dumps(tags or [], ensure_ascii=False), (row.get("updated") or "")[:23], time.time()))


def retry(op):
    o = get(op)
    if o and o["status"] == "error":
        _mark(op, status="queued")
    return send(op)


def pending():
    with db() as d:
        rows = d.run("SELECT id FROM agent_q WHERE status<>'sent' ORDER BY created DESC LIMIT 20", many=True) or []
    return [get(r[0]) for r in rows]


def _loop():
    time.sleep(170)
    while True:
        try:
            now = time.time()
            with db() as d:
                ids = [r[0] for r in d.run("SELECT id FROM agent_q WHERE status='queued' AND attempts<%s AND next_try<=%s AND created<%s",
                                           (MAX_AUTO, now, now - 60), many=True) or []]
            for i in ids:
                send(i)
        except Exception as e:
            print("Контрагенты (очередь):", e, flush=True)
        time.sleep(RETRY_EVERY)


if os.environ.get("PORT"):
    threading.Thread(target=_loop, daemon=True).start()
