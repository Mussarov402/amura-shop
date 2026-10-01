"""Приём заказов с сайта AMURA (сервис amura-shop-api на Render, см. app.py).

Подключение (уже сделано в app.py):
    from order_hook import bp as order_bp
    app.register_blueprint(order_bp)

Маршруты:
    GET  /catalog                  — живой каталог для сайта (МойСклад, кэш 150 сек)
    POST /order                    — заказ с сайта → МойСклад + Telegram владельцу + ссылка на PDF
    GET  /order/<номер>/pdf?t=...  — PDF-накладная (по подписанной ссылке)
    POST /tg/<TG_WEBHOOK_SECRET>   — вебхук бота заказов: /start <номер>_<подпись> → бот шлёт PDF

Переменные окружения:
    MS_TOKEN            токен МойСклад
    MOBIZON_API_KEY     ключ API mobizon.kz (вход по SMS)
    MOBIZON_FROM        имя отправителя SMS (необязательно)
    SMS_DAILY_LIMIT     максимум SMS в день (по умолчанию 300)
    ORDER_BOT_TOKEN     токен бота заказов (отдельный бот, напр. @amura_orders_bot)
    OWNER_CHAT_ID       chat_id владельца в Telegram
    TG_WEBHOOK_SECRET   случайная строка для адреса вебхука
    ORDER_SECRET        случайная строка для подписи ссылок на PDF
    PUBLIC_URL          https://amura-shop-api.onrender.com
    SITE_URL            https://mussarov402.github.io/amura-shop   (оттуда берётся img/index.json)
"""
import hashlib
import hmac
import io
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, Response, jsonify, request
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

API = "https://api.moysklad.ru/api/remap/1.2"
MS_TOKEN = os.environ.get("MS_TOKEN", "")
BOT = os.environ.get("ORDER_BOT_TOKEN", "")
OWNER = os.environ.get("OWNER_CHAT_ID", "")
HOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "")
ORDER_SECRET = os.environ.get("ORDER_SECRET", "").encode()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")

TIER_MID = 10                  # цена типа «От 15шт» действует от 10 шт
LIVE_TTL = 150                 # сек: как часто сервер перечитывает МойСклад для сайта
PRICE_OPT, PRICE_MID, PRICE_BOX = "Оптовая цена", ("От 10шт", "От 15шт"), "Короб"
LOADER_CODE = "00308"          # «Услуга грузчика» (товар в МойСклад)
LOADER_PRICE = 1000
FEE_RATE = 0.0095
FEE_NAME = "Комиссия банка"
SHIPPING = {
    "kamaz": ("КАМАЗ", True), "rail": ("ЖД", True), "avia": ("Авиа", True),
    "kazpost": ("Казпочта", False), "courier": ("Курьер по городу", False), "pickup": ("Самовывоз", False),
}
ALMATY = timezone(timedelta(hours=5))

bp = Blueprint("orders", __name__)
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {MS_TOKEN}", "Accept-Encoding": "gzip",
                  "Content-Type": "application/json"})

_here = os.path.dirname(os.path.abspath(__file__))
pdfmetrics.registerFont(TTFont("DV", os.path.join(_here, "fonts", "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DVB", os.path.join(_here, "fonts", "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFontFamily("DV", normal="DV", bold="DVB", italic="DV", boldItalic="DVB")


# ---------- helpers ----------
def fmt(n):
    return f"{int(round(n)):,}".replace(",", " ")


def sign(number):
    return hmac.new(ORDER_SECRET, str(number).encode(), hashlib.sha256).hexdigest()[:12]


def ms(method, path, **kw):
    url = path if path.startswith("http") else API + path
    tmo = kw.pop("timeout", 30)
    for attempt in range(3):
        t0 = time.time()
        try:
            r = S.request(method, url, timeout=(10, tmo), **kw)
        except requests.RequestException as e:
            print(f"МойСклад {method} {path[:60]} — нет ответа за {time.time() - t0:.0f} с: {e.__class__.__name__}", flush=True)
            if attempt == 2:
                raise
            time.sleep(2)
            continue
        if time.time() - t0 > 5:
            print(f"МойСклад {method} {path[:60]} — {time.time() - t0:.1f} с, {len(r.content)} байт", flush=True)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"МойСклад {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}
    raise RuntimeError("МойСклад не отвечает")


def meta(entity, eid):
    return {"meta": {"href": f"{API}/entity/{entity}/{eid}", "type": entity, "mediaType": "application/json"}}


def tg(method, **data):
    if not BOT:
        return None
    files = data.pop("_files", None)
    r = requests.post(f"https://api.telegram.org/bot{BOT}/{method}", data=data, files=files, timeout=30)
    return r.json()


# ---------- кэш каталога и справочников ----------
_cache = {}


def cached(key, ttl, fn):
    v = _cache.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def _price(row, name):
    names = (name,) if isinstance(name, str) else name
    for p in row.get("salePrices", []):
        if p.get("priceType", {}).get("name") in names:
            return round(p.get("value", 0) / 100)
    return 0


def _brand(row):
    for a in row.get("attributes", []) or []:
        if a.get("name") == "Бренд":
            v = a.get("value")
            return v.get("name") if isinstance(v, dict) else str(v)
    return (row.get("pathName") or "").split("/")[0].strip()


def _img_index():
    try:
        return requests.get(f"{SITE_URL}/img/index.json", timeout=15).json()
    except Exception:
        return {}


def _countries():
    rows = ms("GET", "/entity/country", params={"limit": 1000}).get("rows", [])
    return {r["id"]: r["name"] for r in rows}


def _country(row):
    href = (row.get("country") or {}).get("meta", {}).get("href", "")
    if not href:
        return ""
    try:
        return cached("countries", 86400, _countries).get(href.rsplit("/", 1)[-1], "")
    except Exception:
        return ""


def _barcode(row):
    for b in row.get("barcodes") or []:
        for v in b.values():
            if isinstance(v, str) and v.isdigit():
                return v
    return ""


def _products():
    """Карточки товаров (названия, цены, упаковки, фото) — из выгрузки catalog.json на сайте.
    Её обновляет GitHub Actions; большой список товаров МойСклад отдаёт серверу Render слишком медленно.
    Остатки при этом берутся из МойСклад напрямую (быстрый отчёт), см. _stock()."""
    r = requests.get(f"{SITE_URL}/catalog.json", timeout=20, headers={"Cache-Control": "no-cache"})
    r.raise_for_status()
    return [{"_site": True, **i} for i in r.json()["items"]]


def _stock():
    """Доступный остаток (остаток − резерв) по всем товарам — лёгкий быстрый отчёт МойСклад."""
    data = ms("GET", "/report/stock/all/current", params={"stockType": "freeStock"}, timeout=12)   # 3 попытки по 12 с < 60 с воркера gunicorn
    rows = data if isinstance(data, list) else data.get("rows", [])
    return {r["assortmentId"]: r.get("freeStock", r.get("stock", 0)) for r in rows}


def build_live(products):
    stock = _stock()
    imgs = cached("imgidx", 1800, _img_index)
    new_since = (datetime.now(ALMATY) - timedelta(days=21)).strftime("%Y-%m-%d")
    items, descs = [], {}
    for r in products:
        if r.get("_site"):                 # строка из catalog.json — цены уже готовы, обновляем только остаток
            qty = int(stock.get(r["id"], 0) or 0)
            if qty > 0:
                items.append({k: v for k, v in r.items() if k not in ("_site", "desc")} | {"qty": qty})
            descs[r["id"]] = r.get("desc", "")
            continue
        if r.get("code") == LOADER_CODE:
            continue
        qty = int(stock.get(r["id"], 0) or 0)
        opt = _price(r, PRICE_OPT)
        descs[r["id"]] = (r.get("description") or "")[:4000]
        if qty <= 0 or opt <= 0:
            continue
        mid, box = _price(r, PRICE_MID), _price(r, PRICE_BOX)
        bq = max([int(p.get("quantity", 0)) for p in (r.get("packs") or []) if p.get("quantity", 0) > 1] or [0])
        upd = r.get("updated", "")
        items.append({
            "id": r["id"], "name": r.get("name", ""), "brand": _brand(r), "code": r.get("code", ""),
            "article": r.get("article", ""), "country": _country(r), "barcode": _barcode(r), "qty": qty,
            "opt": opt, "mid": mid if 0 < mid < opt else 0,
            "box": box if (0 < box < opt and bq) else 0, "boxQty": bq if (0 < box < opt) else 0,
            "img": f"img/{r['id']}.webp" if r["id"] in imgs else None,
            "updated": upd[:10], "isNew": upd[:10] >= new_since,
        })
    _cache["descs"] = (time.time(), descs)
    # описание не входит в живой каталог (он уходит клиентам каждые 3 минуты) — отдаётся по /product/<id>
    return {"updated": datetime.now(ALMATY).strftime("%d.%m.%Y %H:%M"), "items": items}


_live_lock = threading.Lock()
_last_hit = [0.0]


def refresh(max_age):
    """Обновляет кэш: карточки раз в 10 минут, остатки — когда данные старше max_age."""
    with _live_lock:
        v = _cache.get("live")
        if v and time.time() - v[0] < max_age:
            return v[1]
        products = cached("products", 600, _products)
        data = build_live(products)
        _cache["live"] = (time.time(), data)
        return data


def _background():
    """Пока сайтом пользуются, держим каталог свежим в фоне — клиенты не ждут МойСклад."""
    while True:
        time.sleep(20)
        if time.time() - _last_hit[0] > 1200:
            continue
        try:
            refresh(LIVE_TTL)
        except Exception as e:
            print("Фоновое обновление каталога:", e, flush=True)


def _probe():
    time.sleep(3)
    try:
        t0 = time.time(); ms("GET", "/entity/organization", params={"limit": 1}, timeout=20)
        print(f"Проверка МойСклад: ответ за {time.time() - t0:.1f} с", flush=True)
        t0 = time.time(); st = _stock()
        print(f"Проверка остатков: {len(st)} позиций за {time.time() - t0:.1f} с", flush=True)
    except Exception as e:
        print("Проверка МойСклад не прошла:", e, flush=True)


threading.Thread(target=_background, daemon=True).start()
threading.Thread(target=_probe, daemon=True).start()


def live(max_age=LIVE_TTL):
    _last_hit[0] = time.time()
    v = _cache.get("live")
    if v and time.time() - v[0] < max_age:
        return v[1]
    if v and max_age >= LIVE_TTL and _live_lock.locked():
        return v[1]                       # обновление уже идёт — отдаём то, что есть
    return refresh(max_age)


def catalog():
    return {i["id"]: i for i in live(max_age=60)["items"]}   # для заказа — остатки не старше минуты


@bp.route("/product/<pid>")
def product_route(pid):
    """Описание товара для карточки (если его нет в catalog.json — например, товар добавлен после выгрузки)."""
    descs = (_cache.get("descs") or (0, {}))[1]
    if pid not in descs:
        if not re.fullmatch(r"[0-9a-f-]{36}", pid):
            return jsonify(error="not found"), 404
        try:
            descs[pid] = (ms("GET", f"/entity/product/{pid}").get("description") or "")[:4000]
        except Exception:
            return jsonify(error="not found"), 404
    resp = jsonify(desc=descs[pid])
    resp.headers["Cache-Control"] = "public, max-age=600"
    return resp


@bp.route("/catalog")
def catalog_route():
    try:
        data = live()
    except Exception as e:
        print("Каталог: ошибка МойСклад:", e, flush=True)
        v = _cache.get("live")
        if not v:
            return jsonify(error=str(e)[:200]), 502
        data = v[1]                        # МойСклад не ответил — отдаём последние данные
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


def organization():
    return cached("org", 86400, lambda: ms("GET", "/entity/organization", params={"limit": 1})["rows"][0]["id"])


def loader_id():
    return cached("loader", 86400, lambda: ms("GET", "/entity/product",
                                                params={"filter": f"code={LOADER_CODE}", "limit": 1})["rows"][0]["id"])


def fee_service_id():
    def find_or_create():
        rows = ms("GET", "/entity/service", params={"filter": f"name={FEE_NAME}", "limit": 1})["rows"]
        if rows:
            return rows[0]["id"]
        return ms("POST", "/entity/service", json={"name": FEE_NAME})["id"]
    return cached("fee", 86400, find_or_create)


def unit_price(item, qty):
    if item.get("box") and item.get("boxQty") and qty >= item["boxQty"]:
        return item["box"]
    if item.get("mid") and qty >= TIER_MID:
        return item["mid"]
    return item["opt"]


def find_or_create_agent(name, phone, telegram, city):
    if phone:
        rows = ms("GET", "/entity/counterparty", params={"search": phone[-10:], "limit": 5})["rows"]
        for r in rows:
            if re.sub(r"\D", "", r.get("phone", ""))[-10:] == phone[-10:]:
                return r["id"]
    if telegram:
        rows = ms("GET", "/entity/counterparty", params={"search": f"@{telegram}", "limit": 5})["rows"]
        for r in rows:
            if f"@{telegram}".lower() in (r.get("description", "") + " " + r.get("name", "")).lower():
                return r["id"]
    body = {"name": f"{name} ({city})", "companyType": "individual", "tags": ["сайт"],
            "actualAddress": city,
            "description": "Клиент с сайта" + (f"\nTelegram: @{telegram}" if telegram else "")}
    if phone:
        body["phone"] = "+" + phone
    return ms("POST", "/entity/counterparty", json=body)["id"]


# ---------- rate limit ----------
_hits = {}
_lock = threading.Lock()


def too_many(ip, limit=5, window=600):
    now = time.time()
    with _lock:
        h = [t for t in _hits.get(ip, []) if now - t < window]
        h.append(now)
        _hits[ip] = h
        return len(h) > limit


# ---------- CORS ----------
@bp.after_request
def cors(resp):
    origin = request.headers.get("Origin", "")
    allowed = {SITE_URL.split("/amura-shop")[0], "https://amura.kz", "https://www.amura.kz"}
    if origin in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, PATCH, OPTIONS"
    return resp


# ---------- POST /order ----------
_orders = {}                  # ключ заказа -> (время, ответ): повтор не создаёт второй заказ
_orders_lock = threading.Lock()


def _order_key(d):
    k = str(d.get("orderKey") or "")[:64]
    if k:
        return "k:" + k
    raw = json.dumps([d.get("phone"), d.get("telegram"), d.get("shipping"),
                      sorted((str(p.get("id")), p.get("qty")) for p in (d.get("items") or []))])
    return "h:" + hashlib.sha1(raw.encode()).hexdigest()


@bp.route("/order", methods=["POST", "OPTIONS"])
def create_order():
    if request.method == "OPTIONS":
        return "", 204
    d = request.get_json(silent=True) or {}
    key = _order_key(d)
    with _orders_lock:                         # один заказ за раз: повторные отправки ждут и получают тот же ответ
        now = time.time()
        for k in [k for k, v in _orders.items() if now - v[0] > 600]:
            del _orders[k]
        if key in _orders:
            return _orders[key][1]
        resp = _create_order_impl()
        if not isinstance(resp, tuple):            # ошибки (tuple) не кэшируем — повтор разрешён
            _orders[key] = (time.time(), resp)
        return resp


def _create_order_impl():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if too_many(ip):
        return jsonify(ok=False, error="Слишком много заказов подряд, подождите 10 минут"), 429
    d = request.get_json(silent=True) or {}
    name = str(d.get("name", "")).strip()[:80]
    city = str(d.get("city", "")).strip()[:80]
    phone = re.sub(r"\D", "", str(d.get("phone", "")))[:15]
    if len(phone) == 11 and phone[0] == "8":
        phone = "7" + phone[1:]
    elif len(phone) == 10:
        phone = "7" + phone
    telegram = re.sub(r"[^A-Za-z0-9_]", "", str(d.get("telegram", "")))[:32]
    ship = d.get("shipping")
    if not name or not city or ship not in SHIPPING or not (len(phone) >= 10 or len(telegram) >= 4):
        return jsonify(ok=False, error="Заполните имя, контакт, город и способ отправки"), 400

    # Цены и остатки пересчитываются на сервере по МойСклад — цены из браузера не используются
    try:
        cat = catalog()
    except Exception as e:
        print("Заказ: МойСклад не ответил по остаткам:", e, flush=True)
        v = _cache.get("live")
        if not v:
            return jsonify(ok=False, error="Склад сейчас не отвечает, попробуйте через минуту"), 503
        cat = {i["id"]: i for i in v[1]["items"]}
    lines = []
    for p in (d.get("items") or [])[:200]:
        item = cat.get(str(p.get("id")))
        qty = int(p.get("qty") or 0)
        if not item or qty <= 0:
            continue
        qty = min(qty, int(item["qty"]))
        lines.append({"id": item["id"], "name": item["name"], "qty": qty, "price": unit_price(item, qty)})
    if not lines:
        return jsonify(ok=False, error="Корзина пуста или товаров нет в наличии"), 400

    ship_name, need_loader = SHIPPING[ship]
    logistics = str(d.get("logistics", "")).strip()[:80]
    recipient = str(d.get("recipient", "")).strip()[:100]
    zipcode = re.sub(r"\D", "", str(d.get("zip", "")))[:6]
    address = str(d.get("address", "")).strip()[:200]
    if need_loader and not logistics:
        return jsonify(ok=False, error="Укажите, через какую логистику отправить"), 400
    if ship == "kazpost" and not (recipient and len(zipcode) == 6 and address):
        return jsonify(ok=False, error="Для Казпочты укажите ФИО, индекс и адрес"), 400
    if ship == "courier" and not address:
        return jsonify(ok=False, error="Укажите адрес доставки по Алматы"), 400
    if ship == "courier":
        ship_name += f" — {address}"
    if need_loader:
        ship_name += f" — {logistics}"
    elif ship == "kazpost":
        ship_name += f" — {recipient}, {zipcode}, {address}"
    goods = sum(l["qty"] * l["price"] for l in lines)
    loader = LOADER_PRICE if need_loader else 0
    fee = int((goods + loader) * FEE_RATE + 0.5)  # как Math.round на сайте
    total = goods + loader + fee

    try:
        me = session_cid()                     # клиент вошёл в «Я» — заказ на его контрагента
        agent = me or find_or_create_agent(name, phone, telegram, city)
        positions = [{"quantity": l["qty"], "price": l["price"] * 100, "assortment": meta("product", l["id"])}
                     for l in lines]
        if loader:
            positions.append({"quantity": 1, "price": loader * 100, "assortment": meta("product", loader_id())})
        positions.append({"quantity": 1, "price": fee * 100, "assortment": meta("service", fee_service_id())})
        contact = f"WhatsApp +{phone}" if phone else f"Telegram @{telegram}"
        order = ms("POST", "/entity/customerorder", json={
            "organization": meta("organization", organization()),
            "agent": meta("counterparty", agent),
            "shipmentAddress": city,
            "description": f"Заказ с сайта\n{name}, {contact}\nГород: {city}\nОтправка: {ship_name}",
            "positions": positions,
        })
    except Exception as e:
        # МойСклад упал — заказ всё равно не теряем: шлём владельцу
        tg("sendMessage", chat_id=OWNER, text=f"⚠️ Заказ с сайта НЕ записан в МойСклад ({e})\n\n{json.dumps(d, ensure_ascii=False)[:3500]}")
        return jsonify(ok=False, error="Не удалось сохранить заказ, менеджер уже получил его и свяжется с вами"), 502

    number = order["name"]
    tok = sign(number)
    data = {"number": number, "date": datetime.now(ALMATY).strftime("%d.%m.%Y %H:%M"), "name": name,
            "contact": contact, "city": city, "ship": ship_name, "lines": lines,
            "loader": loader, "fee": fee, "total": total}
    pdf = build_pdf(data)
    caption = (f"🛒 Заказ с сайта № {number}\n{name} · {contact}\n{city} · {ship_name}\n\n"
               + "\n".join(f"{l['name']} — {l['qty']} × {fmt(l['price'])} = {fmt(l['qty'] * l['price'])} ₸" for l in lines)
               + (f"\nУслуга грузчика — {fmt(loader)} ₸" if loader else "")
               + f"\n{FEE_NAME} 0,95% — {fmt(fee)} ₸\nИтого: {fmt(total)} ₸")
    if len(caption) > 1000:
        tg("sendMessage", chat_id=OWNER, text=caption[:4000])
        caption = f"Заказ № {number}, итого {fmt(total)} ₸"
    try:
        tg("sendDocument", chat_id=OWNER, caption=caption, _files={"document": (f"AMURA-{number}.pdf", pdf, "application/pdf")})
    except Exception as e:                     # заказ уже в МойСклад — сбой Telegram не должен вызвать повторную отправку
        print("Заказ", number, "Telegram не ответил:", e, flush=True)

    return jsonify(ok=True, number=number, total=total, startToken=f"{number}_{tok}",
                   pdfUrl=f"{PUBLIC_URL}/order/{number}/pdf?t={tok}")


# ---------- PDF ----------
def order_from_ms(number):
    rows = ms("GET", "/entity/customerorder", params={"filter": f"name={number}", "limit": 1,
                                                      "expand": "positions.assortment,agent"})["rows"]
    if not rows:
        return None
    o = rows[0]
    desc = (o.get("description") or "").split("\n")
    lines, loader, fee = [], 0, 0
    for p in o["positions"]["rows"]:
        a, price, qty = p["assortment"], p["price"] / 100, p["quantity"]
        if a.get("code") == LOADER_CODE:
            loader += price * qty
        elif a["meta"]["type"] == "service" and a.get("name") == FEE_NAME:
            fee += price * qty
        else:
            lines.append({"name": a["name"], "qty": int(qty), "price": price})
    who = desc[1] if len(desc) > 1 else o["agent"]["name"]
    return {"number": o["name"],
            "date": datetime.strptime(o["moment"][:16], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M"),
            "name": who.split(",")[0], "contact": ",".join(who.split(",")[1:]).strip(),
            "city": (desc[2].replace("Город: ", "") if len(desc) > 2 else ""),
            "ship": (desc[3].replace("Отправка: ", "") if len(desc) > 3 else ""),
            "lines": lines, "loader": loader, "fee": fee, "total": o["sum"] / 100}


def build_pdf(o):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"AMURA — заказ {o['number']}")
    h = ParagraphStyle("h", fontName="DVB", fontSize=15, leading=19)
    n = ParagraphStyle("n", fontName="DV", fontSize=9.5, leading=13)
    c = ParagraphStyle("c", fontName="DV", fontSize=9, leading=11.5)
    el = [Paragraph(f"Заказ покупателя № {o['number']} от {o['date']}", h), Spacer(1, 4 * mm),
          Paragraph("<b>Поставщик:</b> AMURA Cosmetics, Казахстан", n),
          Paragraph(f"<b>Покупатель:</b> {o['name']}, {o['contact']}", n),
          Paragraph(f"<b>Город:</b> {o['city']} &nbsp;&nbsp; <b>Отправка:</b> {o['ship']}", n), Spacer(1, 5 * mm)]
    rows = [["№", "Товар", "Кол-во", "Цена, ₸", "Сумма, ₸"]]
    for i, l in enumerate(o["lines"], 1):
        rows.append([str(i), Paragraph(l["name"], c), fmt(l["qty"]), fmt(l["price"]), fmt(l["qty"] * l["price"])])
    k = len(rows)
    if o["loader"]:
        rows.append(["", "Услуга грузчика", "1", fmt(o["loader"]), fmt(o["loader"])])
    rows.append(["", f"{FEE_NAME} 0,95%", "", "", fmt(o["fee"])])
    rows.append(["", "Итого к оплате", "", "", fmt(o["total"])])
    t = Table(rows, colWidths=[9 * mm, 95 * mm, 18 * mm, 28 * mm, 30 * mm], repeatRows=1)
    t.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), "DV", 9), ("FONT", (0, 0), (-1, 0), "DVB", 9), ("FONT", (0, -1), (-1, -1), "DVB", 10),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0E3B2C")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, k - 1), 0.4, colors.HexColor("#C9D3CE")),
        ("LINEABOVE", (0, k), (-1, k), 0.8, colors.HexColor("#0E3B2C")),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor("#0E3B2C")),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    el += [t, Spacer(1, 6 * mm), Paragraph("Цены указаны в тенге. Менеджер свяжется с вами для подтверждения, оплаты и отправки.", n)]
    doc.build(el)
    return buf.getvalue()


@bp.route("/order/<number>/pdf")
def order_pdf(number):
    if not hmac.compare_digest(sign(number), request.args.get("t", "")):
        return "Ссылка недействительна", 403
    o = order_from_ms(number)
    if not o:
        return "Заказ не найден", 404
    return Response(build_pdf(o), mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="AMURA-{number}.pdf"'})


# ---------- бот: /start <номер>_<подпись> ----------
@bp.route("/tg/<secret>", methods=["POST"])
def tg_webhook(secret):
    if not HOOK_SECRET or not hmac.compare_digest(secret, HOOK_SECRET):
        return "", 403
    u = request.get_json(silent=True) or {}
    if u.get("callback_query"):
        handle_owner_callback(u["callback_query"])
        return "", 200
    msg = u.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    text = msg.get("text", "")
    if not chat:
        return "", 200
    lm = re.match(r"^/start\s+login_([A-Za-z0-9]{20,40})$", text.strip())
    if lm:
        handle_tg_login(lm.group(1), msg.get("from") or {}, chat)
        return "", 200
    m = re.match(r"^/start\s+(\S+?)_([0-9a-f]{12})$", text.strip())
    if m and hmac.compare_digest(sign(m.group(1)), m.group(2)):
        o = order_from_ms(m.group(1))
        if o:
            tg("sendDocument", chat_id=chat,
               caption=f"Ваш заказ AMURA № {o['number']} на {fmt(o['total'])} ₸. Менеджер свяжется с вами для оплаты и отправки.",
               _files={"document": (f"AMURA-{o['number']}.pdf", build_pdf(o), "application/pdf")})
            user = (msg.get("from") or {}).get("username", "")
            tg("sendMessage", chat_id=OWNER, text=f"Клиент @{user or chat} получил накладную по заказу № {o['number']}")
            return "", 200
    tg("sendMessage", chat_id=chat, text="Здравствуйте! Это бот заказов AMURA. Оформите заказ на сайте — и накладная придёт сюда.")
    return "", 200


# =====================================================================
#  Раздел «Я»: вход по SMS-коду (Mobizon) или через Telegram, история заказов.
#  Все данные клиента — в МойСклад (контрагент с тегом «сайт» + доп. поле «Сайт: Telegram ID»).
#  Сессия — подписанный токен (без базы данных).
# =====================================================================
import base64
import secrets

SESSION_DAYS = 180
ATTR_TGID = "Сайт: Telegram ID"
MOBIZON_KEY = os.environ.get("MOBIZON_API_KEY", "")
MOBIZON_FROM = os.environ.get("MOBIZON_FROM", "")            # имя отправителя, если зарегистрировано
SMS_DAILY_LIMIT = int(os.environ.get("SMS_DAILY_LIMIT", "300"))
SMS_TEXT = "AMURA: код для входа {code}. Никому не сообщайте его."


def _b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def make_token(cid):
    body = _b64(json.dumps({"c": cid, "e": int(time.time()) + SESSION_DAYS * 86400}).encode())
    return body + "." + hmac.new(ORDER_SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]


def session_cid():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    try:
        body, sig = h[7:].split(".")
        if not hmac.compare_digest(sig, hmac.new(ORDER_SECRET, body.encode(), hashlib.sha256).hexdigest()[:32]):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data["c"] if data["e"] > time.time() else None
    except Exception:
        return None


def tg_attr():
    def load():
        rows = ms("GET", "/entity/counterparty/metadata/attributes").get("rows", [])
        for r in rows:
            if r["name"] == ATTR_TGID:
                return r
        return ms("POST", "/entity/counterparty/metadata/attributes", json={"name": ATTR_TGID, "type": "string", "required": False})
    return cached("tgattr", 86400, load)


def set_tg_id(cid, tg_id):
    ms("PUT", f"/entity/counterparty/{cid}", json={"attributes": [{"meta": tg_attr()["meta"], "value": str(tg_id)}]})


def norm_phone(v):
    d = re.sub(r"\D", "", str(v or ""))[:15]
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    elif len(d) == 10:
        d = "7" + d
    return d


def cp_by_phone(phone):
    for r in ms("GET", "/entity/counterparty", params={"search": phone[-10:], "limit": 10})["rows"]:
        if norm_phone(r.get("phone", ""))[-10:] == phone[-10:]:
            return r
    return None


def cp_by_tg(tg_id, username):
    href = tg_attr()["meta"]["href"]
    rows = ms("GET", "/entity/counterparty", params={"filter": f"{href}={tg_id}", "limit": 1})["rows"]
    if rows:
        return rows[0]
    if username:
        for r in ms("GET", "/entity/counterparty", params={"search": f"@{username}", "limit": 5})["rows"]:
            if f"@{username}".lower() in (r.get("description", "") + " " + r.get("name", "")).lower():
                return r
    return None


def profile(cp):
    desc = cp.get("description") or ""
    tgu = re.search(r"Telegram: @(\w+)", desc)
    name = re.sub(r"\s*\([^)]*\)$", "", cp.get("name", ""))
    return {"name": "" if name == "Новый клиент сайта" else name, "phone": norm_phone(cp.get("phone", "")),
            "telegram": tgu.group(1) if tgu else "", "city": cp.get("actualAddress", "") or ""}


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


# ---------- вход по SMS ----------
_codes = {}            # phone -> {"h": hash кода, "t": время, "tries": n}
_sms_day = {"d": "", "n": 0}


def send_sms(phone, text):
    params = {"output": "json", "api": "v1", "apiKey": MOBIZON_KEY, "recipient": phone, "text": text}
    if MOBIZON_FROM:
        params["from"] = MOBIZON_FROM
    r = requests.post("https://api.mobizon.kz/service/message/sendsmsmessage", data=params, timeout=20).json()
    if r.get("code") != 0:
        raise RuntimeError(r.get("message") or "SMS не отправлено")


@bp.route("/auth/sms/send", methods=["POST", "OPTIONS"])
def auth_sms_send():
    if request.method == "OPTIONS":
        return "", 204
    if not MOBIZON_KEY:
        return jsonify(ok=False, error="Вход по SMS пока недоступен. Войдите через Telegram"), 503
    phone = norm_phone((request.get_json(silent=True) or {}).get("phone"))
    if not re.fullmatch(r"77\d{9}", phone):
        return jsonify(ok=False, error="Укажите казахстанский номер: +7 7__ ___ __ __"), 400
    if too_many("smsip:" + client_ip(), limit=5, window=3600) or too_many("smsph:" + phone, limit=3, window=3600):
        return jsonify(ok=False, error="Слишком много кодов. Попробуйте через час или войдите через Telegram"), 429
    prev = _codes.get(phone)
    if prev and time.time() - prev["t"] < 60:
        return jsonify(ok=False, error="Код уже отправлен, подождите минуту"), 429
    today = datetime.now(ALMATY).strftime("%Y-%m-%d")
    if _sms_day["d"] != today:
        _sms_day.update(d=today, n=0)
    if _sms_day["n"] >= SMS_DAILY_LIMIT:
        return jsonify(ok=False, error="Вход по SMS временно недоступен. Войдите через Telegram"), 503
    code = f"{secrets.randbelow(1000000):06d}"
    try:
        send_sms(phone, SMS_TEXT.format(code=code))
    except Exception as e:
        return jsonify(ok=False, error=f"Не удалось отправить SMS: {e}"), 502
    _sms_day["n"] += 1
    if _sms_day["n"] == SMS_DAILY_LIMIT:
        tg("sendMessage", chat_id=OWNER, text=f"⚠️ Сайт: за сегодня отправлено {SMS_DAILY_LIMIT} SMS — лимит, вход по SMS остановлен до завтра")
    _codes[phone] = {"h": hashlib.sha256((code + phone).encode()).hexdigest(), "t": time.time(), "tries": 0}
    return jsonify(ok=True)


@bp.route("/auth/sms/verify", methods=["POST", "OPTIONS"])
def auth_sms_verify():
    if request.method == "OPTIONS":
        return "", 204
    d = request.get_json(silent=True) or {}
    phone, code = norm_phone(d.get("phone")), re.sub(r"\D", "", str(d.get("code", "")))
    v = _codes.get(phone)
    if not v or time.time() - v["t"] > 600:
        return jsonify(ok=False, error="Код устарел, запросите новый"), 400
    v["tries"] += 1
    if v["tries"] > 5:
        _codes.pop(phone, None)
        return jsonify(ok=False, error="Слишком много попыток, запросите новый код"), 429
    if not hmac.compare_digest(v["h"], hashlib.sha256((code + phone).encode()).hexdigest()):
        return jsonify(ok=False, error="Неверный код"), 400
    _codes.pop(phone, None)
    cp = cp_by_phone(phone)       # номер подтверждён SMS — если клиент уже покупал, увидит свою историю
    new = cp is None
    if new:
        cp = ms("POST", "/entity/counterparty", json={
            "name": "Новый клиент сайта", "companyType": "individual", "tags": ["сайт"],
            "phone": "+" + phone, "description": "Клиент с сайта"})
    elif "сайт" not in (cp.get("tags") or []):
        ms("PUT", f"/entity/counterparty/{cp['id']}", json={"tags": sorted(set((cp.get("tags") or []) + ["сайт"]))})
    return jsonify(ok=True, token=make_token(cp["id"]), needProfile=new or not profile(cp)["city"])


# ---------- вход через Telegram ----------
_tg_login = {}   # nonce -> {"t": время, "token": ..., "new": bool}


@bp.route("/auth/tg/start", methods=["POST", "OPTIONS"])
def auth_tg_start():
    if request.method == "OPTIONS":
        return "", 204
    if too_many("tg:" + client_ip(), limit=20, window=600):
        return jsonify(ok=False, error="Слишком много попыток"), 429
    now = time.time()
    for k in [k for k, v in _tg_login.items() if now - v["t"] > 900]:
        _tg_login.pop(k, None)
    nonce = secrets.token_urlsafe(18).replace("-", "a").replace("_", "b")
    _tg_login[nonce] = {"t": now, "token": None, "new": False}
    return jsonify(ok=True, nonce=nonce)


@bp.route("/auth/tg/poll")
def auth_tg_poll():
    v = _tg_login.get(request.args.get("nonce", ""))
    if not v:
        return jsonify(ok=False, error="Ссылка устарела, нажмите «Войти через Telegram» ещё раз"), 404
    if v["token"]:
        _tg_login.pop(request.args["nonce"], None)
        return jsonify(ok=True, token=v["token"], needProfile=v["new"])
    return jsonify(ok=True, token=None)


def handle_tg_login(nonce, user, chat):
    v = _tg_login.get(nonce)
    if not v:
        tg("sendMessage", chat_id=chat, text="Ссылка для входа устарела. Нажмите «Войти через Telegram» на сайте ещё раз.")
        return
    tg_id, username = str(user.get("id", chat)), user.get("username", "")
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or username or "Клиент"
    cp = cp_by_tg(tg_id, username)
    if cp:
        cid, new = cp["id"], not profile(cp)["city"]
    else:
        cid, new = ms("POST", "/entity/counterparty", json={
            "name": name, "companyType": "individual", "tags": ["сайт"],
            "description": "Клиент с сайта" + (f"\nTelegram: @{username}" if username else f"\nTelegram ID: {tg_id}")})["id"], True
    set_tg_id(cid, tg_id)
    v.update(token=make_token(cid), new=new)
    tg("sendMessage", chat_id=chat, text="Готово, вы вошли в AMURA ✅\nВернитесь на сайт — личный кабинет уже открыт.",
       reply_markup=json.dumps({"inline_keyboard": [[{"text": "Открыть сайт", "url": SITE_URL + "/#me"}]]}))


def handle_owner_callback(cq):
    return None   # подтверждение владельцем больше не нужно — номер подтверждает SMS


# ---------- профиль и история ----------
@bp.route("/me", methods=["GET", "PATCH", "OPTIONS"])
def me():
    if request.method == "OPTIONS":
        return "", 204
    cid = session_cid()
    if not cid:
        return jsonify(ok=False, error="Войдите заново"), 401
    if request.method == "PATCH":
        d = request.get_json(silent=True) or {}
        name, city = str(d.get("name", "")).strip()[:80], str(d.get("city", "")).strip()[:80]
        if not name or not city:
            return jsonify(ok=False, error="Укажите имя и город"), 400
        cur = ms("GET", f"/entity/counterparty/{cid}")
        body = {"name": f"{name} ({city})", "actualAddress": city}
        ph = norm_phone(d.get("phone"))
        if ph and not cur.get("phone"):          # телефон, подтверждённый SMS, не меняем
            body["phone"] = "+" + ph
        ms("PUT", f"/entity/counterparty/{cid}", json=body)
        if not cur.get("actualAddress"):        # первое заполнение профиля — сообщаем владельцу
            tg("sendMessage", chat_id=OWNER, text=f"🆕 Клиент на сайте: {name}, {city}" + (f", {cur.get('phone') or ('+' + ph if ph else '')}"))
    cp = ms("GET", f"/entity/counterparty/{cid}")
    orders = []
    rows = ms("GET", "/entity/customerorder", params={
        "filter": f"agent={API}/entity/counterparty/{cid}", "order": "moment,desc", "limit": 30,
        "expand": "state,positions.assortment"})["rows"]
    for o in rows:
        items, count = [], 0
        for p in o.get("positions", {}).get("rows", []):
            a = p.get("assortment", {})
            if a.get("meta", {}).get("type") == "product" and a.get("code") != LOADER_CODE:
                items.append({"id": a["id"], "name": a.get("name", ""), "qty": int(p["quantity"]), "price": p["price"] / 100})
                count += int(p["quantity"])
        orders.append({"number": o["name"], "date": datetime.strptime(o["moment"][:16], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y"),
                       "status": (o.get("state") or {}).get("name", "Новый"), "sum": o["sum"] / 100,
                       "count": count, "items": items,
                       "pdfUrl": f"{PUBLIC_URL}/order/{o['name']}/pdf?t={sign(o['name'])}"})
    return jsonify(ok=True, profile=profile(cp), orders=orders)
