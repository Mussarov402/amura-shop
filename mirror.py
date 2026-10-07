"""Зеркало МойСклад в нашей базе (этап 1 миграции, см. docs/MIGRATION.md) — ТОЛЬКО ЧТЕНИЕ из МойСклад.
Таблицы ms_*: товары и модификации с ценами и штрихкодами, склады, остатки по складам, состояние синхронизации, сверки.
Фоновая синхронизация работает, только если в таблице setting включён флаг feat_mirror_sync (по умолчанию выключен).
Бережно к лимитам МойСклад: запросы по одному через oh.ms, небольшие страницы, пауза между страницами,
за один проход — не больше MAX_PAGES страниц; первичная загрузка растягивается на несколько проходов."""
import json
import os
import threading
import time

import inbox
import order_hook as oh

FLAG = "feat_mirror_sync"
PAGE = 200                 # строк на страницу (товары с ценами — тяжёлые, большие страницы МойСклад отдаёт медленно)
MAX_PAGES = 10             # страниц одной сущности за проход
PAUSE = 1.0                # сек между страницами
EVERY = 600                # сек между проходами, когда всё загружено
EVERY_LOADING = 120        # сек между проходами во время первичной загрузки
FULL_EVERY = 86400         # раз в сутки — полный проход: находим удалённые в МойСклад товары
ENTITIES = ("product", "variant")

_ready = [False]
_run_lock = threading.Lock()


def _schema(d):
    if _ready[0]:
        return
    pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    for s in (
        # kind: product | variant; prices — JSON {тип цены: тенге}; barcodes — JSON-список; updated — время МойСклад (Москва)
        "CREATE TABLE IF NOT EXISTS ms_product (id TEXT PRIMARY KEY, kind TEXT, parent_id TEXT, code TEXT, article TEXT, name TEXT,"
        " folder TEXT, archived INTEGER DEFAULT 0, prices TEXT, buy_price DOUBLE PRECISION, barcodes TEXT, updated TEXT,"
        " deleted INTEGER DEFAULT 0, seen DOUBLE PRECISION)",
        "CREATE TABLE IF NOT EXISTS ms_store (id TEXT PRIMARY KEY, name TEXT, archived INTEGER DEFAULT 0, updated TEXT, seen DOUBLE PRECISION)",
        "CREATE TABLE IF NOT EXISTS ms_stock (product_id TEXT, store_id TEXT, stock DOUBLE PRECISION, reserve DOUBLE PRECISION,"
        " synced DOUBLE PRECISION, PRIMARY KEY (product_id, store_id))",
        # cursor — последнее загруженное updated (до секунды), skip — сколько строк с этим же updated уже загружено;
        # full_from — начало текущего полного прохода (0 — идёт обычная докачка изменений)
        "CREATE TABLE IF NOT EXISTS ms_sync (entity TEXT PRIMARY KEY, cursor TEXT, skip INTEGER DEFAULT 0, full_from DOUBLE PRECISION DEFAULT 0,"
        " full_done DOUBLE PRECISION DEFAULT 0, last_run DOUBLE PRECISION, last_ok DOUBLE PRECISION, rows INTEGER DEFAULT 0, error TEXT)",
        f"CREATE TABLE IF NOT EXISTS ms_recon (id {pk}, at DOUBLE PRECISION, ok INTEGER, body TEXT)",
    ):
        d.run(s)
    d.c.commit()
    _ready[0] = True


def db():
    d = inbox.db()
    _schema(d)
    return d


def enabled():
    try:
        with inbox.db() as d:
            return inbox.get_setting(d, FLAG, "0") == "1"
    except Exception:
        return False


def set_enabled(on):
    with inbox.db() as d:
        inbox.set_setting(d, FLAG, "1" if on else "0")


# ---------- разбор строк МойСклад ----------
def _id(row):
    return row.get("id") or (row.get("meta") or {}).get("href", "").rsplit("/", 1)[-1]


def _href_id(obj):
    return ((obj or {}).get("meta") or {}).get("href", "").rsplit("/", 1)[-1].split("?")[0]


def _prices(row):
    out = {}
    for p in row.get("salePrices") or []:
        name = (p.get("priceType") or {}).get("name")
        if name:
            out[name] = round((p.get("value") or 0) / 100)
    return out


def _barcodes(row):
    return [v for b in row.get("barcodes") or [] for v in b.values() if isinstance(v, str)]


def _parse(kind, row, now):
    return (_id(row), kind, _href_id(row.get("product")) if kind == "variant" else None,
            row.get("code") or "", row.get("article") or "", row.get("name") or "",
            row.get("pathName") or "", 1 if row.get("archived") else 0,
            json.dumps(_prices(row), ensure_ascii=False),
            round(((row.get("buyPrice") or {}).get("value") or 0) / 100),
            json.dumps(_barcodes(row), ensure_ascii=False), (row.get("updated") or "")[:23], 0, now)


_COLS = "id, kind, parent_id, code, article, name, folder, archived, prices, buy_price, barcodes, updated, deleted, seen"


def _upsert(d, table, cols, key, rows):
    """Вставка или обновление по ключу (одинаково для Postgres и SQLite)."""
    names = [c.strip() for c in cols.split(",")]
    keys = [k.strip() for k in key.split(",")]
    sets = ", ".join(f"{c}=excluded.{c}" for c in names if c not in keys)
    ph = ", ".join(["%s"] * len(names))
    for r in rows:
        d.run(f"INSERT INTO {table} ({cols}) VALUES ({ph}) ON CONFLICT ({key}) DO UPDATE SET {sets}", r)


# ---------- синхронизация ----------
def _state(d, entity):
    r = d.run("SELECT cursor, skip, full_from, full_done FROM ms_sync WHERE entity=%s", (entity,), one=True)
    if not r:
        d.run("INSERT INTO ms_sync (entity, cursor, skip, full_from, full_done, rows) VALUES (%s, '', 0, 0, 0, 0)", (entity,))
        return {"cursor": "", "skip": 0, "full_from": 0.0, "full_done": 0.0}
    return {"cursor": r[0] or "", "skip": r[1] or 0, "full_from": r[2] or 0.0, "full_done": r[3] or 0.0}


def _save_state(d, entity, st, **extra):
    vals = {"cursor": st["cursor"], "skip": st["skip"], "full_from": st["full_from"], "full_done": st["full_done"], **extra}
    d.run("UPDATE ms_sync SET " + ", ".join(f"{k}=%s" for k in vals) + " WHERE entity=%s", (*vals.values(), entity))


def sync_entity(entity, max_pages=MAX_PAGES, pause=PAUSE):
    """Догрузка изменённых строк сущности по возрастанию updated. Возвращает (загружено строк, догнали ли МойСклад).
    Пагинация «по ключу»: filter updated>=cursor и offset=skip — число уже загруженных строк ровно с этим updated,
    поэтому новые изменения в МойСклад во время загрузки не сдвигают страницы и строки не теряются."""
    now = time.time()
    with db() as d:
        st = _state(d, entity)
        if not st["full_from"] and now - st["full_done"] > FULL_EVERY:
            st.update(cursor="", skip=0, full_from=now)              # новый полный проход
            _save_state(d, entity, st)
    total, caught = 0, False
    for _ in range(max_pages):
        params = {"limit": PAGE, "offset": st["skip"], "order": "updated,asc"}
        if st["cursor"]:
            params["filter"] = f"updated>={st['cursor']}"
        rows = oh.ms("GET", f"/entity/{entity}", params=params, timeout=60).get("rows", [])
        t = time.time()
        with db() as d:
            _upsert(d, "ms_product", _COLS, "id", [_parse(entity, r, t) for r in rows])
            for r in rows:
                u = (r.get("updated") or "")[:19]
                if u > st["cursor"]:
                    st["cursor"], st["skip"] = u, 1
                elif u == st["cursor"]:
                    st["skip"] += 1
            total += len(rows)
            if len(rows) < PAGE:
                caught = True
                if st["full_from"]:                                   # полный проход закончен: чего не видели — удалено в МойСклад
                    d.run("UPDATE ms_product SET deleted=1 WHERE kind=%s AND (seen IS NULL OR seen<%s)", (entity, st["full_from"]))
                    st.update(full_from=0.0, full_done=t)
            _save_state(d, entity, st, last_run=t, last_ok=t, error="")
            d.run("UPDATE ms_sync SET rows=(SELECT COUNT(*) FROM ms_product WHERE kind=%s AND deleted=0) WHERE entity=%s", (entity, entity))
        if caught:
            break
        time.sleep(pause)
    return total, caught


def sync_stores():
    rows = oh.ms("GET", "/entity/store", params={"limit": 1000}, timeout=30).get("rows", [])
    t = time.time()
    with db() as d:
        _upsert(d, "ms_store", "id, name, archived, updated, seen", "id",
                [(_id(r), r.get("name") or "", 1 if r.get("archived") else 0, (r.get("updated") or "")[:23], t) for r in rows])
        st = _state(d, "store")
        _save_state(d, "store", st, last_run=t, last_ok=t, rows=len(rows), error="")
    return len(rows)


def _stock_report(kind):
    data = oh.ms("GET", "/report/stock/bystore/current", params={"stockType": kind}, timeout=60)
    rows = data if isinstance(data, list) else data.get("rows", [])
    return {(r["assortmentId"], r["storeId"]): r.get(kind, 0) or 0 for r in rows}


def sync_stock():
    """Остатки по складам целиком (лёгкий отчёт «текущие остатки»): заменяем таблицу за одну транзакцию."""
    stock = _stock_report("stock")
    reserve = _stock_report("reserve")
    t = time.time()
    with db() as d:
        d.run("DELETE FROM ms_stock")
        for (pid, sid) in set(stock) | set(reserve):
            d.run("INSERT INTO ms_stock (product_id, store_id, stock, reserve, synced) VALUES (%s, %s, %s, %s, %s)",
                  (pid, sid, stock.get((pid, sid), 0), reserve.get((pid, sid), 0), t))
        st = _state(d, "stock")
        _save_state(d, "stock", st, last_run=t, last_ok=t, rows=len(set(stock) | set(reserve)), error="")
    return len(stock)


def _fail(entity, e):
    print("Зеркало МойСклад:", entity, e.__class__.__name__, str(e)[:200], flush=True)
    try:
        with db() as d:
            st = _state(d, entity)
            _save_state(d, entity, st, last_run=time.time(), error=f"{e.__class__.__name__}: {str(e)[:300]}")
    except Exception:
        pass


def tick():
    """Один проход синхронизации. Возвращает True, если всё догнали (первичная загрузка закончена)."""
    if not _run_lock.acquire(blocking=False):
        return False
    try:
        caught_all = True
        for name, fn in (("store", sync_stores), *((e, (lambda e=e: sync_entity(e))) for e in ENTITIES), ("stock", sync_stock)):
            try:
                r = fn()
                if isinstance(r, tuple) and not r[1]:
                    caught_all = False
            except Exception as e:
                caught_all = False
                _fail(name, e)
        return caught_all
    finally:
        _run_lock.release()


def running():
    return _run_lock.locked()


def tick_bg():
    if running():
        return False
    threading.Thread(target=tick, daemon=True).start()
    return True


def _loop():
    time.sleep(90)
    nxt = 0.0
    while True:
        try:
            if enabled() and time.time() >= nxt:
                nxt = time.time() + (EVERY if tick() else EVERY_LOADING)
        except Exception as e:
            print("Зеркало МойСклад (цикл):", e, flush=True)
        time.sleep(30)


if os.environ.get("PORT"):          # только на сервере; сам цикл ничего не делает, пока флаг выключен
    threading.Thread(target=_loop, daemon=True).start()


# ---------- просмотр и сверка ----------
def status():
    with db() as d:
        rows = d.run("SELECT entity, cursor, full_from, full_done, last_run, last_ok, rows, error FROM ms_sync", many=True) or []
        rec = d.run("SELECT at, ok, body FROM ms_recon ORDER BY id DESC LIMIT 1", one=True)
        flag = inbox.get_setting(d, FLAG, "0") == "1"
    ent = {r[0]: {"cursor": r[1] or "", "loading": bool(r[2]) or not r[3], "fullDone": r[3] or 0, "lastRun": r[4] or 0,
                  "lastOk": r[5] or 0, "rows": r[6] or 0, "error": r[7] or ""} for r in rows}
    return {"enabled": flag, "running": running(), "reconRunning": _recon_lock.locked(), "entities": ent,
            "recon": ({"at": rec[0], "ok": bool(rec[1]), **json.loads(rec[2] or "{}")} if rec else None)}


def _ms_count(entity, flt="archived=false"):
    r = oh.ms("GET", f"/entity/{entity}", params={"filter": flt, "limit": 1}, timeout=30)
    return (r.get("meta") or {}).get("size", 0)


def _check(name, ours, theirs, unit="", detail=None, count=False):
    """count=True — проверка-счётчик: ours — число расхождений, должно быть 0."""
    return {"name": name, "ours": ours, "theirs": theirs, "ok": ours == theirs, "unit": unit, "detail": detail or [], "count": count}


def reconcile():
    """Сверка «наши цифры = МойСклад»: количество товаров и модификаций, остатки по товарам, цены сайта.
    Пишет результат в ms_recon и возвращает его."""
    checks = []
    with db() as d:
        for entity, title in (("product", "Товары (не в архиве)"), ("variant", "Модификации (не в архиве)")):
            ours = d.run("SELECT COUNT(*) FROM ms_product WHERE kind=%s AND deleted=0 AND archived=0", (entity,), one=True)[0]
            checks.append(_check(title, ours, _ms_count(entity), "шт"))
        ours_stock = {}
        for pid, q in d.run("SELECT product_id, SUM(stock) FROM ms_stock GROUP BY product_id", many=True) or []:
            ours_stock[pid] = round(q or 0, 3)
        names = dict(d.run("SELECT id, name FROM ms_product", many=True) or [])
        prices = {r[0]: json.loads(r[1] or "{}") for r in d.run("SELECT id, prices FROM ms_product WHERE deleted=0", many=True) or []}
    data = oh.ms("GET", "/report/stock/all/current", params={"stockType": "stock"}, timeout=60)
    theirs_stock = {r["assortmentId"]: round(r.get("stock", 0) or 0, 3) for r in (data if isinstance(data, list) else data.get("rows", []))}
    bad = sorted((pid for pid in set(ours_stock) | set(theirs_stock) if ours_stock.get(pid, 0) != theirs_stock.get(pid, 0)),
                 key=lambda p: -abs(ours_stock.get(p, 0) - theirs_stock.get(p, 0)))
    checks.append(_check("Остаток, всего единиц", round(sum(ours_stock.values())), round(sum(theirs_stock.values())), "шт",
                         [f"{names.get(p, p)}: у нас {ours_stock.get(p, 0):g}, в МойСклад {theirs_stock.get(p, 0):g}" for p in bad[:10]]))
    checks.append(_check("Товары с другим остатком", len(bad), 0, count=True))
    v = oh._cache.get("live")                                         # цены, которые сейчас видит сайт
    if v:
        diff = []
        for i in v[1].get("items", []):
            p = prices.get(i.get("id"))
            if p is None:
                diff.append(f"{i.get('name', i.get('id'))}: нет в нашей базе")
                continue
            for key, ptype in (("rtl", "Розничная цена"), ("opt", "Оптовая цена")):
                if i.get(key) and p.get(ptype, 0) != round(i[key]):
                    diff.append(f"{i.get('name')}: {ptype.lower()} у нас {p.get(ptype, 0)} ₸, на сайте {round(i[key])} ₸")
        checks.append(_check("Цены товаров сайта", len(diff), 0, "", diff[:10], count=True))
    res = {"checks": checks}
    ok = all(c["ok"] for c in checks)
    with db() as d:
        d.run("INSERT INTO ms_recon (at, ok, body) VALUES (%s, %s, %s)", (time.time(), 1 if ok else 0, json.dumps(res, ensure_ascii=False)))
    return {"ok": ok, **res}


_recon_lock = threading.Lock()


def recon_bg():
    """Сверка в фоне (несколько запросов к МойСклад дольше таймаута запроса панели); результат — в status()."""
    if _recon_lock.locked():
        return False

    def run():
        with _recon_lock:
            try:
                reconcile()
            except Exception as e:
                _fail("recon", e)
    threading.Thread(target=run, daemon=True).start()
    return True


def stock_of(q, limit=30):
    """Поиск по зеркалу: товары с остатками по складам (для будущего раздела «Склад»)."""
    q = (q or "").strip()
    like, raw = f"%{q.lower()}%", f"%{q}%"
    with db() as d:
        stores = dict(d.run("SELECT id, name FROM ms_store", many=True) or [])
        rows = d.run("SELECT id, code, name, prices FROM ms_product WHERE deleted=0 AND (LOWER(name) LIKE %s OR name LIKE %s OR code LIKE %s"
                     " OR barcodes LIKE %s) ORDER BY name LIMIT %s", (like, raw, raw, raw, limit), many=True) or []
        out = []
        for pid, code, name, pr in rows:
            st = d.run("SELECT store_id, stock, reserve FROM ms_stock WHERE product_id=%s", (pid,), many=True) or []
            out.append({"id": pid, "code": code, "name": name, "prices": json.loads(pr or "{}"),
                        "stores": [{"store": stores.get(s, s), "stock": a or 0, "reserve": b or 0} for s, a, b in st]})
    return out
