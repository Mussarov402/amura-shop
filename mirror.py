"""Зеркало МойСклад в нашей базе (этап 1 миграции, см. docs/MIGRATION.md) — ТОЛЬКО ЧТЕНИЕ из МойСклад.
Таблицы ms_*: товары и модификации с ценами и штрихкодами, склады, остатки по складам, состояние синхронизации, сверки.
Фоновая синхронизация работает, только если в таблице setting включён флаг feat_mirror_sync (по умолчанию выключен).
Бережно к лимитам МойСклад: запросы по одному через oh.ms, небольшие страницы, пауза между страницами,
за один проход — не больше MAX_PAGES страниц; первичная загрузка растягивается на несколько проходов."""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import inbox
import order_hook as oh

FLAG = "feat_mirror_sync"
PAGE = 200                 # строк на страницу (товары с ценами — тяжёлые, большие страницы МойСклад отдаёт медленно)
MAX_PAGES = 10             # страниц одной сущности за проход
PAUSE = 1.0                # сек между страницами
EVERY = 600                # сек между проходами, когда всё загружено
EVERY_LOADING = 120        # сек между проходами во время первичной загрузки
FULL_EVERY = 86400         # раз в сутки — полный проход: находим удалённые в МойСклад товары
DOC_TYPES = ("customerorder", "demand", "retaildemand", "salesreturn", "retailsalesreturn")   # документы продаж
DOC_DAYS = 90              # документы берём за последние 90 дней по updated (вся история не нужна для сверки и тяжела)
ENTITIES = ("product", "variant", "counterparty") + DOC_TYPES
MS_TZ = timezone(timedelta(hours=3))   # время в МойСклад — Москва
RECON_EVERY = 86400        # автосверка раз в сутки, когда загрузка догнала МойСклад

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
        # контрагенты: tags — JSON-список тегов МойСклад; company_type — legal / entrepreneur / individual
        "CREATE TABLE IF NOT EXISTS ms_agent (id TEXT PRIMARY KEY, name TEXT, phone TEXT, email TEXT, inn TEXT, company_type TEXT,"
        " tags TEXT, archived INTEGER DEFAULT 0, updated TEXT, deleted INTEGER DEFAULT 0, seen DOUBLE PRECISION)",
        # шапки документов продаж: type — сущность МойСклад; moment/updated — время МойСклад (Москва); sum — тенге;
        # applicable — проведён; state_id — статус (у заказов); descr — первая строка комментария (источник заказа)
        "CREATE TABLE IF NOT EXISTS ms_doc (id TEXT PRIMARY KEY, type TEXT, number TEXT, moment TEXT, agent_id TEXT, store_id TEXT,"
        " state_id TEXT, sum DOUBLE PRECISION, applicable INTEGER, descr TEXT, updated TEXT, deleted INTEGER DEFAULT 0, seen DOUBLE PRECISION)",
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
_ACOLS = "id, name, phone, email, inn, company_type, tags, archived, updated, deleted, seen"


def _parse_agent(row, now):
    return (_id(row), row.get("name") or "", row.get("phone") or "", row.get("email") or "", row.get("inn") or "",
            row.get("companyType") or "", json.dumps(row.get("tags") or [], ensure_ascii=False),
            1 if row.get("archived") else 0, (row.get("updated") or "")[:23], 0, now)


_DCOLS = "id, type, number, moment, agent_id, store_id, state_id, sum, applicable, descr, updated, deleted, seen"


def _doc_parser(kind):
    def parse(row, now):
        return (_id(row), kind, row.get("name") or "", (row.get("moment") or "")[:23], _href_id(row.get("agent")),
                _href_id(row.get("store")), _href_id(row.get("state")), (row.get("sum") or 0) / 100,
                1 if row.get("applicable") else 0, (row.get("description") or "").split("\n")[0][:200],
                (row.get("updated") or "")[:23], 0, now)
    return parse


def _floor(entity):
    """Нижняя граница updated для полного прохода: у документов — DOC_DAYS дней назад, у справочников — без границы."""
    if entity not in DOC_TYPES:
        return ""
    return (datetime.now(MS_TZ) - timedelta(days=DOC_DAYS)).strftime("%Y-%m-%d %H:%M:%S")


# сущность МойСклад -> (таблица, колонки, разбор строки, условие «строки этой сущности» в таблице)
_TABLES = {
    "product": ("ms_product", _COLS, lambda r, t: _parse("product", r, t), "kind='product'"),
    "variant": ("ms_product", _COLS, lambda r, t: _parse("variant", r, t), "kind='variant'"),
    "counterparty": ("ms_agent", _ACOLS, _parse_agent, "1=1"),
    **{t: ("ms_doc", _DCOLS, _doc_parser(t), f"type='{t}'") for t in DOC_TYPES},
}


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
    table, cols, parse, mine = _TABLES[entity]
    now = time.time()
    with db() as d:
        st = _state(d, entity)
        if not st["full_from"] and now - st["full_done"] > FULL_EVERY:
            st.update(cursor=_floor(entity), skip=0, full_from=now)  # новый полный проход
            _save_state(d, entity, st)
    total, caught = 0, False
    for _ in range(max_pages):
        params = {"limit": PAGE, "offset": st["skip"], "order": "updated,asc"}
        if st["cursor"]:
            params["filter"] = f"updated>={st['cursor']}"
        _site_room()
        rows = oh.ms("GET", f"/entity/{entity}", params=params, timeout=60).get("rows", [])
        t = time.time()
        with db() as d:
            _upsert(d, table, cols, "id", [parse(r, t) for r in rows])
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
                    fl = _floor(entity)                               # (у документов — только в окне полного прохода)
                    d.run(f"UPDATE {table} SET deleted=1 WHERE {mine} AND (seen IS NULL OR seen<%s) AND COALESCE(updated, '')>=%s",
                          (st["full_from"], fl))
                    st.update(full_from=0.0, full_done=t)
            _save_state(d, entity, st, last_run=t, last_ok=t, error="")
            d.run(f"UPDATE ms_sync SET rows=(SELECT COUNT(*) FROM {table} WHERE {mine} AND deleted=0) WHERE entity=%s", (entity,))
        if caught:
            break
        time.sleep(pause)
    return total, caught


def _site_room(wait=30):
    """Сайт и заказы важнее зеркала: перед запросом ждём (до wait с), пока в ограничителе oh.ms свободно
    хотя бы 2 места — одно наше, одно останется сайту."""
    end = time.time() + wait
    while getattr(oh.MS_PARALLEL, "_value", 2) < 2 and time.time() < end:
        time.sleep(0.5)


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
        caught_all, parts, t0 = True, [], time.time()
        for name, fn in (("store", sync_stores), *((e, (lambda e=e: sync_entity(e))) for e in ENTITIES), ("stock", sync_stock)):
            try:
                r = fn()
                if isinstance(r, tuple):
                    parts.append(f"{name} +{r[0]}" + ("" if r[1] else " (загрузка)"))
                    if not r[1]:
                        caught_all = False
                else:
                    parts.append(f"{name} {r}")
            except Exception as e:
                caught_all = False
                parts.append(f"{name} ОШИБКА")
                _fail(name, e)
        # итог прохода — в логи Render (база снаружи закрыта): видно, что зеркало живо и догнало ли МойСклад
        print(f"Зеркало МойСклад, проход: {'; '.join(parts)}; {time.time() - t0:.0f} с; догнали: {'да' if caught_all else 'нет'}", flush=True)
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


def _autostart():
    """Решение владельца (07.10.2026): зеркало включается само один раз. Если ключ уже есть (в т.ч. выключено вручную) — не трогаем."""
    try:
        with inbox.db() as d:
            if inbox.get_setting(d, FLAG, "") == "":
                inbox.set_setting(d, FLAG, "1")
                print("Зеркало МойСклад: включено автоматически (первый запуск)", flush=True)
    except Exception as e:
        print("Зеркало МойСклад: автозапуск не удался:", e, flush=True)


def _loop():
    time.sleep(90)
    _autostart()
    print("Зеркало МойСклад: цикл запущен, загрузка", "включена" if enabled() else "выключена", flush=True)
    try:
        log_last_recon()
    except Exception as e:
        print("Зеркало МойСклад: итог сверки не прочитан:", e, flush=True)
    nxt = 0.0
    while True:
        try:
            if enabled() and time.time() >= nxt:
                done = tick()
                nxt = time.time() + (EVERY if done else EVERY_LOADING)
                if done and time.time() - last_recon() > RECON_EVERY:
                    reconcile()
        except Exception as e:
            print("Зеркало МойСклад (цикл):", e, flush=True)
        time.sleep(30)


if os.environ.get("PORT"):          # только на сервере; сам цикл ничего не делает, пока флаг выключен
    threading.Thread(target=_loop, daemon=True).start()


# ---------- просмотр и сверка ----------
def last_recon():
    with db() as d:
        r = d.run("SELECT MAX(at) FROM ms_recon", one=True)
    return (r and r[0]) or 0


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


SALE_TYPES, RETURN_TYPES = ("demand", "retaildemand"), ("salesreturn", "retailsalesreturn")


def _day_bounds(day):
    """Сутки по Алматы -> границы во времени МойСклад (строки, сравниваются как moment)."""
    a = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=oh.ALMATY)
    f = lambda t: t.astimezone(MS_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return f(a), f(a + timedelta(days=1))


def _ms_docs(entity, flt):
    out, off = [], 0
    while True:
        r = oh.ms("GET", f"/entity/{entity}", params={"filter": flt, "limit": 1000, "offset": off}, timeout=60)
        out += r.get("rows", [])
        off += 1000
        if off >= (r.get("meta") or {}).get("size", 0) or off >= 10000:
            return out


def _sales_checks():
    """Продажи за вчера и сегодня (Алматы), как на «Обзоре»: проведённые отгрузки + чеки − возвраты; и число заказов."""
    out = []
    today = datetime.now(oh.ALMATY).date()
    for day in (str(today - timedelta(days=1)), str(today)):
        a, b = _day_bounds(day)
        label = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m")
        with db() as d:
            ours = {t: d.run("SELECT COUNT(*), COALESCE(SUM(sum), 0) FROM ms_doc WHERE type=%s AND deleted=0 AND applicable=1"
                             " AND moment>=%s AND moment<%s", (t, a, b), one=True) for t in SALE_TYPES + RETURN_TYPES}
            ours_orders = d.run("SELECT COUNT(*) FROM ms_doc WHERE type='customerorder' AND deleted=0 AND moment>=%s AND moment<%s",
                                (a, b), one=True)[0]
        theirs = {}
        for t in SALE_TYPES + RETURN_TYPES:
            rows = _ms_docs(t, f"moment>={a};moment<{b};applicable=true")
            theirs[t] = (len(rows), sum((r.get("sum") or 0) for r in rows) / 100)
        net = lambda m: round(sum(m[t][1] for t in SALE_TYPES) - sum(m[t][1] for t in RETURN_TYPES))
        cnt = lambda m: sum(m[t][0] for t in SALE_TYPES + RETURN_TYPES)
        out.append(_check(f"Продажи {label}: отгрузки + чеки − возвраты", net(ours), net(theirs), "₸"))
        out.append(_check(f"Документы продаж {label}", cnt(ours), cnt(theirs), "шт"))
        out.append(_check(f"Заказы покупателей {label}", ours_orders, _ms_count("customerorder", f"moment>={a};moment<{b}"), "шт"))
    return out


def reconcile():
    """Сверка «наши цифры = МойСклад»: количество товаров и модификаций, остатки по товарам, цены сайта.
    Пишет результат в ms_recon и возвращает его."""
    checks = []
    with db() as d:
        for entity, title in (("product", "Товары (не в архиве)"), ("variant", "Модификации (не в архиве)"),
                              ("counterparty", "Контрагенты (не в архиве)")):
            table, _, _, mine = _TABLES[entity]
            ours = d.run(f"SELECT COUNT(*) FROM {table} WHERE {mine} AND deleted=0 AND archived=0", one=True)[0]
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
    checks += _sales_checks()
    res = {"checks": checks}
    ok = all(c["ok"] for c in checks)
    with db() as d:
        d.run("INSERT INTO ms_recon (at, ok, body) VALUES (%s, %s, %s)", (time.time(), 1 if ok else 0, json.dumps(res, ensure_ascii=False)))
    _log_recon(ok, checks)
    return {"ok": ok, **res}


def _log_recon(ok, checks, when=""):
    """Итог сверки в логи Render (база снаружи закрыта): у нас/МойСклад по каждой проверке и примеры расхождений."""
    print(f"Зеркало МойСклад, сверка{when}:", "OK" if ok else "РАСХОЖДЕНИЯ",
          "; ".join(f"{c['name']}: {c['ours']}/{c['theirs']}" for c in checks), flush=True)
    for c in checks:
        if not c["ok"] and c.get("detail"):
            print(f"Зеркало МойСклад, сверка{when} — {c['name']}:", " | ".join(c["detail"][:5]), flush=True)


def log_last_recon():
    """При запуске цикла — итог последней сохранённой сверки (новая будет не раньше чем через RECON_EVERY)."""
    with db() as d:
        r = d.run("SELECT at, ok, body FROM ms_recon ORDER BY id DESC LIMIT 1", one=True)
    if not r:
        print("Зеркало МойСклад, сверка: ещё не было", flush=True)
        return
    when = datetime.fromtimestamp(r[0], oh.ALMATY).strftime("%d.%m %H:%M")
    _log_recon(bool(r[1]), json.loads(r[2] or "{}").get("checks", []), f" (последняя, {when} Алматы)")


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
