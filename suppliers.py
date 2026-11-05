"""Поставщики на опте (решение владельца 05.11.2026, docs/MARKETPLACE.md): товары соседей-поставщиков на opt.amura.kz.
Не маркетплейс с комиссией, а перепродажа: AMURA продаёт товар соседа под своим именем по цене соседа + наша наценка,
при заказе — покупает у соседа. Покупатель поставщика не видит.

Этап 1 (этот модуль): карточки поставщиков, источники остатков и цен (МойСклад поставщика только на чтение,
Google-таблица / CSV по ссылке, Excel загрузкой), синхронизация раз в 15 минут, сопоставление с нашими товарами
по штрихкоду, «На проверке» для несовпавших (на витрину — только после «Добавить» владельца: создаётся карточка
в МойСклад). Витрина опта и заказы — следующие шаги.

Цена товара соседа: (цена соседа − скидка поставщика + наценка ₸) / (1 − сумма % расходов), вверх до 10 ₸.
Токены поставщиков — в нашей базе зашифрованными (ключ выводится из секрета сервера), в ответах API — только маска,
в журнал не пишутся. Цены и наценку задаёт только владелец (все маршруты — только для владельца)."""
import base64
import csv
import hashlib
import io
import json
import math
import os
import re
import threading
import time

import requests
from flask import Blueprint, jsonify, request

import bg
import inbox
import modules
import order_hook as oh

bp = Blueprint("suppliers", __name__)
MOD = "suppliers"
SYNC_EVERY = 900                     # 15 минут
MS_API = os.environ.get("SUPPLIER_MS_API", "https://api.moysklad.ru/api/remap/1.2").rstrip("/")
SOURCES = {"ms": "МойСклад поставщика (токен только на чтение)", "sheet": "Google-таблица или CSV по ссылке", "file": "Excel или CSV файлом"}
FEES = {"acq": ("Эквайринг", 3.5), "tax": ("Налог", 2.0), "cash": ("Снятие", 1.0)}
DEFAULT_MARKUP = 150
NOTE_DEFAULT = "под заказ, 1–2 дня"
_ready = [False]
_lock = threading.Lock()


# ---------- база ----------
def db():
    d = inbox.db()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        d.run(f"CREATE TABLE IF NOT EXISTS supplier (id {pk}, name TEXT, contact TEXT DEFAULT '', markup INTEGER DEFAULT {DEFAULT_MARKUP},"
              " discount INTEGER DEFAULT 0, src TEXT DEFAULT 'sheet', url TEXT DEFAULT '', secret TEXT DEFAULT '', price_type TEXT DEFAULT '',"
              " active INTEGER DEFAULT 1, created DOUBLE PRECISION, synced DOUBLE PRECISION DEFAULT 0, error TEXT DEFAULT '',"
              " ms_agent TEXT DEFAULT '')")
        d.run("CREATE TABLE IF NOT EXISTS sup_item (sup_id INTEGER, barcode TEXT, name TEXT, stock DOUBLE PRECISION, price DOUBLE PRECISION,"
              " updated DOUBLE PRECISION, PRIMARY KEY (sup_id, barcode))")
        d.run("CREATE TABLE IF NOT EXISTS sup_review (sup_id INTEGER, barcode TEXT, state TEXT, product_id TEXT DEFAULT '',"
              " at DOUBLE PRECISION, PRIMARY KEY (sup_id, barcode))")      # added / hidden
        d.c.commit()
        _ready[0] = True
    return d


COLS = "id, name, contact, markup, discount, src, url, secret, price_type, active, created, synced, error, ms_agent"


def _row(r):
    return dict(zip(COLS.split(", "), r)) if r else None


def get(sid):
    with db() as d:
        return _row(d.run(f"SELECT {COLS} FROM supplier WHERE id=%s", (int(sid),), one=True))


def all_suppliers():
    with db() as d:
        return [_row(r) for r in d.run(f"SELECT {COLS} FROM supplier ORDER BY id", many=True) or []]


# ---------- шифрование токенов ----------
def _fernet():
    from cryptography.fernet import Fernet
    base = os.environ.get("SUPPLIER_SECRET") or oh.ORDER_SECRET or ""
    if not base:
        raise RuntimeError("на сервере нет секрета для шифрования (ORDER_SECRET)")
    key = hashlib.sha256(("amura-suppliers:" + base).encode()).digest()       # отдельный ключ, не сам секрет
    return Fernet(base64.urlsafe_b64encode(key))


def seal(token):
    return _fernet().encrypt(token.encode()).decode() if token else ""


def unseal(blob):
    return _fernet().decrypt(blob.encode()).decode() if blob else ""


def mask(blob):
    if not blob:
        return ""
    try:
        t = unseal(blob)
    except Exception:
        return "•••• (не расшифровывается — впишите заново)"
    return "••••" + t[-4:] if len(t) > 8 else "••••"


# ---------- цена ----------
def fees():
    with db() as d:
        try:
            v = json.loads(inbox.get_setting(d, "sup_fees", "") or "{}")
        except Exception:
            v = {}
    return {k: float(v.get(k, dflt)) for k, (_, dflt) in FEES.items()}


def note():
    with db() as d:
        return inbox.get_setting(d, "sup_note", NOTE_DEFAULT) or NOTE_DEFAULT


def sale_price(cost, markup=DEFAULT_MARKUP, discount=0, f=None):
    """(цена соседа − скидка поставщика + наценка ₸) / (1 − сумма % расходов), вверх до 10 ₸."""
    f = fees() if f is None else f
    share = sum(f.values()) / 100
    if cost is None or cost <= 0 or share >= 0.9:
        return 0
    base = max(float(cost) - float(discount or 0), 0) + float(markup or 0)
    return int(math.ceil(round(base / (1 - share), 6) / 10) * 10)


# ---------- источники ----------
HEADS = {"barcode": ("штрихкод", "штрих-код", "штрих код", "barcode", "ean", "шк"),
         "name": ("название", "наименование", "товар", "name"),
         "stock": ("остаток", "остатки", "количество", "кол-во", "stock", "qty"),
         "price": ("цена", "price", "стоимость")}


def _num(v):
    s = re.sub(r"[^\d,.\-]", "", str(v or "")).replace(",", ".")
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _bar(v):
    s = str(v or "").strip()
    if re.fullmatch(r"\d+\.0", s):                       # Excel отдаёт 8809...0.0
        s = s[:-2]
    s = re.sub(r"\D", "", s)
    return s if 8 <= len(s) <= 14 else ""


def parse_rows(rows):
    """Таблица (первая строка — заголовки) → [{barcode, name, stock, price}]. Колонки узнаём по названию."""
    rows = [list(r) for r in rows if r and any(str(c or "").strip() for c in r)]
    if not rows:
        return []
    head = [str(c or "").strip().lower() for c in rows[0]]
    idx = {k: next((i for i, h in enumerate(head) if any(h.startswith(w) for w in words)), None) for k, words in HEADS.items()}
    if idx["barcode"] is None or idx["price"] is None:
        raise ValueError("в таблице нужны колонки «Штрихкод» и «Цена» (и желательно «Название», «Остаток»)")
    out = {}
    for r in rows[1:]:
        cell = lambda k: r[idx[k]] if idx[k] is not None and idx[k] < len(r) else ""
        b = _bar(cell("barcode"))
        if not b:
            continue
        out[b] = {"barcode": b, "name": str(cell("name") or "").strip()[:200], "stock": max(_num(cell("stock")), 0) if idx["stock"] is not None else 0,
                  "price": _num(cell("price"))}
    return list(out.values())


def sheet_url(url):
    """Ссылка на Google-таблицу → выгрузка CSV (gid листа сохраняем)."""
    m = re.match(r"https://docs\.google\.com/spreadsheets/d/([\w-]+)", url or "")
    if m:
        gid = re.search(r"[#&?]gid=(\d+)", url)
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv" + (f"&gid={gid.group(1)}" if gid else "")
    return url


def from_sheet(url):
    if not re.match(r"https://", url or ""):
        raise ValueError("ссылка должна начинаться с https://")
    r = requests.get(sheet_url(url), timeout=30)
    if r.status_code >= 400:
        raise ValueError(f"таблица не открылась (HTTP {r.status_code}) — откройте доступ «всем, у кого есть ссылка»")
    text = r.content.decode("utf-8-sig", errors="replace")
    if text.lstrip().startswith("<"):
        raise ValueError("по ссылке не таблица, а страница — откройте доступ «всем, у кого есть ссылка»")
    dialect = ";" if text.count(";") > text.count(",") else ","
    return parse_rows(csv.reader(io.StringIO(text), delimiter=dialect))


def from_file(data, name):
    name = (name or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        return parse_rows(wb.worksheets[0].iter_rows(values_only=True))
    text = data.decode("utf-8-sig", errors="replace")
    dialect = ";" if text.count(";") > text.count(",") else ","
    return parse_rows(csv.reader(io.StringIO(text), delimiter=dialect))


def from_ms(token, price_type=""):
    """МойСклад поставщика (его токен, права только на чтение): товары с остатком, штрихкодом и ценой продажи."""
    out, off = {}, 0
    h = {"Authorization": "Bearer " + token, "Accept-Encoding": "gzip"}
    while True:
        r = requests.get(MS_API + "/entity/assortment", headers=h, timeout=40,
                         params={"limit": 1000, "offset": off, "filter": "archived=false"})
        if r.status_code in (401, 403):
            raise ValueError("МойСклад поставщика не пустил: проверьте токен и права на чтение товаров и остатков")
        if r.status_code >= 400:
            raise ValueError(f"МойСклад поставщика ответил HTTP {r.status_code}")
        rows = r.json().get("rows") or []
        for x in rows:
            bar = next((_bar(v) for b in x.get("barcodes") or [] for v in b.values() if _bar(v)), "")
            if not bar:
                continue
            prices = x.get("salePrices") or []
            p = next((s for s in prices if price_type and (s.get("priceType") or {}).get("name") == price_type), prices[0] if prices else {})
            out[bar] = {"barcode": bar, "name": str(x.get("name") or "")[:200], "stock": max(float(x.get("stock") or 0), 0),
                        "price": float(p.get("value") or 0) / 100}
        if len(rows) < 1000 or off > 50000:
            return list(out.values())
        off += 1000


# ---------- синхронизация ----------
def store(sid, items):
    now = time.time()
    with db() as d:
        d.run("DELETE FROM sup_item WHERE sup_id=%s", (sid,))
        for it in items:
            d.run("INSERT INTO sup_item (sup_id, barcode, name, stock, price, updated) VALUES (%s,%s,%s,%s,%s,%s)",
                  (sid, it["barcode"], it["name"], it["stock"], it["price"], now))
        d.run("UPDATE supplier SET synced=%s, error='' WHERE id=%s", (now, sid))


def sync_one(s):
    """Забрать остатки и цены одного поставщика. Ошибку пишем в карточку (без токена и без тела ответа)."""
    try:
        if s["src"] == "ms":
            items = from_ms(unseal(s["secret"]), s["price_type"])
        elif s["src"] == "sheet":
            items = from_sheet(s["url"])
        else:
            return None                                   # файл — только загрузкой
        store(s["id"], items)
        return len(items)
    except Exception as e:
        msg = str(e)[:300]
        with db() as d:
            d.run("UPDATE supplier SET error=%s, synced=%s WHERE id=%s", (msg, time.time(), s["id"]))
        print(f"Поставщик {s['id']}: остатки не получены: {msg[:150]}", flush=True)
        return None


def tick():
    if not modules.enabled(MOD):
        return
    with _lock:
        for s in all_suppliers():
            if s["active"] and s["src"] != "file" and time.time() - (s["synced"] or 0) >= SYNC_EVERY - 30:
                sync_one(s)


def _loop():
    while True:
        time.sleep(60)
        try:
            tick()
        except Exception as e:
            print("Поставщики: синхронизация:", str(e)[:200], flush=True)


def start():
    bg.start(_loop)


# ---------- сопоставление ----------
def our_items():
    """Наш каталог: штрихкод → товар (id, название, остаток, оптовая цена)."""
    out = {}
    for it in (oh.live() or {}).get("items") or []:
        b = _bar(it.get("barcode"))
        if b:
            out[b] = it
    return out


def match(sid=None):
    """Позиции поставщиков: совпавшие с нашими по штрихкоду и «на проверке»."""
    ours = our_items()
    sups = {s["id"]: s for s in all_suppliers()}
    with db() as d:
        q = "SELECT sup_id, barcode, name, stock, price FROM sup_item" + (" WHERE sup_id=%s" if sid else "")
        rows = d.run(q, (int(sid),) if sid else (), many=True) or []
        rev = {(a, b): (st, pid) for a, b, st, pid in d.run("SELECT sup_id, barcode, state, product_id FROM sup_review", many=True) or []}
    f = fees()
    matched, review = [], []
    for sup_id, bar, name, stock, price in rows:
        s = sups.get(sup_id)
        if not s:
            continue
        x = {"sup": sup_id, "supName": s["name"], "barcode": bar, "name": name, "stock": stock, "cost": price,
             "price": sale_price(price, s["markup"], s["discount"], f)}
        it = ours.get(bar)
        if it:
            x.update(ourId=it.get("id"), ourName=it.get("name"), ourStock=it.get("qty", 0))
            matched.append(x)
        else:
            st = rev.get((sup_id, bar), ("", ""))[0]
            if st != "hidden":
                x["state"] = st or "new"
                review.append(x)
    return matched, review


# ---------- система AMURA: API (только владелец) ----------
def _owner():
    import admin
    if not modules.enabled(MOD):
        return None, (jsonify(ok=False, error="Модуль «Поставщики» выключен"), 404)
    me = admin.who()
    if not me:
        return None, (jsonify(ok=False, error="Войдите заново"), 401)
    if me["role"] != "owner":
        return None, (jsonify(ok=False, error="Поставщики — только для владельца"), 403)
    return me, None


def _public(s):
    with db() as d:
        n = d.run("SELECT COUNT(*) FROM sup_item WHERE sup_id=%s", (s["id"],), one=True)[0]
    return {"id": s["id"], "name": s["name"], "contact": s["contact"], "markup": s["markup"], "discount": s["discount"],
            "src": s["src"], "srcName": SOURCES.get(s["src"], s["src"]), "url": s["url"], "token": mask(s["secret"]),
            "priceType": s["price_type"], "active": bool(s["active"]), "synced": s["synced"], "error": s["error"], "items": n}


@bp.get("/admin/api/suppliers")
def route_list():
    _, err = _owner()
    if err:
        return err
    matched, review = match()
    return jsonify(ok=True, suppliers=[_public(s) for s in all_suppliers()], fees=fees(),
                   feeNames={k: v[0] for k, v in FEES.items()}, note=note(), sources=SOURCES,
                   matched=matched[:500], review=review[:500], counts={"matched": len(matched), "review": len(review)})


def _clean(b, old=None):
    old = old or {}
    name = str(b.get("name", old.get("name", "")) or "").strip()[:80]
    if not name:
        raise ValueError("Назовите поставщика")
    src = b.get("src", old.get("src", "sheet"))
    if src not in SOURCES:
        raise ValueError("Источник: МойСклад, таблица по ссылке или файл")
    try:
        markup = int(b.get("markup", old.get("markup", DEFAULT_MARKUP)))
        discount = int(b.get("discount", old.get("discount", 0)) or 0)
    except (TypeError, ValueError):
        raise ValueError("Наценка и скидка — целые числа, ₸")
    if not 0 <= markup <= 100000 or not 0 <= discount <= 100000:
        raise ValueError("Наценка и скидка — от 0 до 100 000 ₸")
    url = str(b.get("url", old.get("url", "")) or "").strip()[:500]
    if src == "sheet" and not url.startswith("https://"):
        raise ValueError("Вставьте ссылку на таблицу (https://…)")
    v = {"name": name, "contact": str(b.get("contact", old.get("contact", "")) or "").strip()[:200], "markup": markup,
         "discount": discount, "src": src, "url": url if src == "sheet" else "",
         "price_type": str(b.get("priceType", old.get("price_type", "")) or "").strip()[:80],
         "active": 1 if b.get("active", bool(old.get("active", 1))) else 0}
    tok = b.get("token")
    if tok:                                             # новый токен — шифруем; пустое поле — оставляем прежний
        v["secret"] = seal(str(tok).strip())
    if src == "ms" and not (v.get("secret") or old.get("secret")):
        raise ValueError("Вставьте токен МойСклад поставщика (сотрудник с правами только на чтение)")
    return v


@bp.post("/admin/api/suppliers")
def route_new():
    _, err = _owner()
    if err:
        return err
    try:
        v = _clean(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    v.setdefault("secret", "")
    with db() as d:
        sid = d.run("INSERT INTO supplier (name, contact, markup, discount, src, url, secret, price_type, active, created) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (v["name"], v["contact"], v["markup"], v["discount"], v["src"], v["url"],
                                                            v["secret"], v["price_type"], v["active"], time.time()), ins=True)
    return jsonify(ok=True, id=sid)


@bp.put("/admin/api/suppliers/<int:sid>")
def route_edit(sid):
    _, err = _owner()
    if err:
        return err
    old = get(sid)
    if not old:
        return jsonify(ok=False, error="Поставщик не найден"), 404
    try:
        v = _clean(request.get_json(silent=True) or {}, old)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    with db() as d:
        d.run(f"UPDATE supplier SET {', '.join(k + '=%s' for k in v)} WHERE id=%s", (*v.values(), sid))
    return jsonify(ok=True)


@bp.delete("/admin/api/suppliers/<int:sid>")
def route_del(sid):
    _, err = _owner()
    if err:
        return err
    with db() as d:
        for t in ("sup_item", "sup_review"):
            d.run(f"DELETE FROM {t} WHERE sup_id=%s", (sid,))
        d.run("DELETE FROM supplier WHERE id=%s", (sid,))
    return jsonify(ok=True)


@bp.post("/admin/api/suppliers/<int:sid>/sync")
def route_sync(sid):
    _, err = _owner()
    if err:
        return err
    s = get(sid)
    if not s:
        return jsonify(ok=False, error="Поставщик не найден"), 404
    if s["src"] == "file":
        up = request.files.get("file")
        if not up:
            return jsonify(ok=False, error="Выберите файл Excel или CSV"), 400
        try:
            items = from_file(up.read(), up.filename)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        except Exception:
            return jsonify(ok=False, error="Файл не прочитался — нужен .xlsx или .csv"), 400
        store(sid, items)
        return jsonify(ok=True, items=len(items))
    n = sync_one(s)
    if n is None:
        return jsonify(ok=False, error=get(sid)["error"] or "Не получилось"), 502
    return jsonify(ok=True, items=n)


@bp.put("/admin/api/suppliers/settings")
def route_settings():
    _, err = _owner()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    with db() as d:
        if "fees" in b:
            try:
                f = {k: float((b["fees"] or {}).get(k, dflt)) for k, (_, dflt) in FEES.items()}
            except (TypeError, ValueError):
                return jsonify(ok=False, error="Проценты — числа"), 400
            if any(not 0 <= v <= 30 for v in f.values()) or sum(f.values()) >= 50:
                return jsonify(ok=False, error="Каждый процент — от 0 до 30, в сумме меньше 50"), 400
            inbox.set_setting(d, "sup_fees", json.dumps(f))
        if "note" in b:
            inbox.set_setting(d, "sup_note", str(b["note"] or "").strip()[:60] or NOTE_DEFAULT)
    return jsonify(ok=True, fees=fees(), note=note())


@bp.post("/admin/api/suppliers/review")
def route_review():
    """«Добавить» — карточка в МойСклад (название, штрихкод), «Скрыть» — не показывать в «На проверке»."""
    _, err = _owner()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    sid, bar, act = int(b.get("sup") or 0), _bar(b.get("barcode")), b.get("action")
    if act not in ("add", "hide") or not bar:
        return jsonify(ok=False, error="Не тот запрос"), 400
    with db() as d:
        it = d.run("SELECT name FROM sup_item WHERE sup_id=%s AND barcode=%s", (sid, bar), one=True)
    if not it:
        return jsonify(ok=False, error="Позиции у поставщика уже нет"), 404
    pid = ""
    if act == "add":
        if bar in our_items():
            return jsonify(ok=False, error="Такой штрихкод уже есть у нас"), 409
        name = str(b.get("name") or it[0] or "").strip()[:250]
        if not name:
            return jsonify(ok=False, error="Укажите название"), 400
        try:
            pid = oh.ms("POST", "/entity/product", json={"name": name, "barcodes": [{"ean13" if len(bar) == 13 else "ean8" if len(bar) == 8 else "code128": bar}],
                                                         "description": "Товар поставщика (опт) — добавлен из «Поставщиков»"}, timeout=20)["id"]
        except Exception as e:
            return jsonify(ok=False, error="МойСклад не создал карточку: " + str(e)[:200]), 502
    with db() as d:
        d.run("DELETE FROM sup_review WHERE sup_id=%s AND barcode=%s", (sid, bar))
        d.run("INSERT INTO sup_review (sup_id, barcode, state, product_id, at) VALUES (%s,%s,%s,%s,%s)",
              (sid, bar, "added" if act == "add" else "hidden", pid, time.time()))
    return jsonify(ok=True, productId=pid)
