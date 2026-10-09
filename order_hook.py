"""Приём заказов с сайта AMURA (сервис amura-shop-api на Render, см. app.py).

Подключение (уже сделано в app.py):
    from order_hook import bp as order_bp
    app.register_blueprint(order_bp)

Маршруты:
    GET  /catalog                  — живой каталог для сайта (МойСклад, кэш 150 сек).
                                     Всем — розничная цена; оптовику (вошёл в «Я», тег «опт» в МойСклад) — опт / от 10 шт / короб
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
    MANAGER_CHAT_IDS    chat_id менеджеров через запятую (получают уведомления по клиентам; узнать: написать боту /id)
    TG_WEBHOOK_SECRET   случайная строка для адреса вебхука
    ORDER_SECRET        случайная строка для подписи ссылок на PDF
    PUBLIC_URL          https://amura-shop-api.onrender.com
    SITE_URL            https://mussarov402.github.io/amura-shop   (оттуда берётся img/index.json)
    WHOLESALE_TAG       тег контрагента в МойСклад, открывающий оптовые цены (по умолчанию «опт»)
"""
import gzip
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

from pricebox import unseal
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
MANAGERS = [x.strip() for x in os.environ.get("MANAGER_CHAT_IDS", "").replace(";", ",").split(",") if x.strip() and x.strip() != OWNER]
HOOK_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "")
ORDER_SECRET = os.environ.get("ORDER_SECRET", "").encode()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")

TIER_MID = 10                  # цена типа «От 10шт» действует от 10 шт
LIVE_TTL = 150                 # сек: как часто сервер перечитывает МойСклад для сайта
PRICE_RTL, PRICE_OPT, PRICE_MID, PRICE_BOX = "Розничная цена", "Оптовая цена", ("От 10шт",), "Короб"
WHOLESALE_TAG = os.environ.get("WHOLESALE_TAG", "опт").strip().lower()
RETAIL_SOURCE = "с розничного сайта"   # «Заказ с розничного сайта» в описании — по нему панель отделяет розничные заказы
RETAIL_MARK = "Заказ " + RETAIL_SOURCE
PUBLIC_WHOLESALE = os.environ.get("PRICE_MODE", "opt") == "opt"   # opt — опт видят все; retail — всем розница, опт по тегу
LOADER_CODE = "00308"          # «Услуга грузчика» (товар в МойСклад)
LOADER_PRICE = 1000
FEE_RATE = 0.0095
FEE_NAME = "Комиссия банка"
SHIPPING = {
    "kamaz": ("КАМАЗ", True), "rail": ("ЖД", True), "avia": ("Авиа", True),
    "kazpost": ("Казпочта", False), "courier": ("Курьер по городу", False), "pickup": ("Самовывоз", False),
    "cdek": ("СДЭК", False), "express": ("Срочный курьер", False),
}
RETAIL_SHIPPING = ("courier", "cdek", "pickup", "express")   # что из этого доступно — по городу (checkout.allowed)   # розничный сайт: без КАМАЗа, ЖД, авиа и Казпочты
RETAIL_TAG = "розница"   # метка покупателя в МойСклад: заходил или заказывал на розничном сайте (панель → Клиенты → «Розница»)
ALMATY = timezone(timedelta(hours=5))

bp = Blueprint("orders", __name__)
S = requests.Session()
S.headers.update({"Authorization": f"Bearer {MS_TOKEN}", "Accept-Encoding": "gzip",
                  "Content-Type": "application/json",
                  "Connection": "close"})   # без «залежавшихся» соединений: на них запрос висит до таймаута

_here = os.path.dirname(os.path.abspath(__file__))
pdfmetrics.registerFont(TTFont("DV", os.path.join(_here, "fonts", "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DVB", os.path.join(_here, "fonts", "DejaVuSans-Bold.ttf")))
pdfmetrics.registerFontFamily("DV", normal="DV", bold="DVB", italic="DV", boldItalic="DVB")


# ---------- helpers ----------
def fmt(n):
    return f"{int(round(n)):,}".replace(",", " ")


def sign(number):
    return hmac.new(ORDER_SECRET, str(number).encode(), hashlib.sha256).hexdigest()[:12]


MS_PARALLEL = threading.BoundedSemaphore(int(os.environ.get("MS_PARALLEL", "4")))   # МойСклад: не больше 5 запросов одновременно


def ms(method, path, **kw):
    url = path if path.startswith("http") else API + path
    tmo = kw.pop("timeout", 30)
    for attempt in range(5):
        t0 = time.time()
        try:
            with MS_PARALLEL:              # при наплыве заказов запросы ждут очереди, а не получают отказ МойСклад
                r = S.request(method, url, timeout=(10, tmo), **kw)
        except requests.RequestException as e:
            print(f"МойСклад {method} {path[:60]} — нет ответа за {time.time() - t0:.0f} с: {e.__class__.__name__}", flush=True)
            if attempt >= 2 or method != "GET":   # запись не повторяем вслепую: МойСклад мог её уже принять (так появлялись дубли)
                raise
            time.sleep(2)
            continue
        if time.time() - t0 > 5:
            print(f"МойСклад {method} {path[:60]} — {time.time() - t0:.1f} с, {len(r.content)} байт", flush=True)
        if r.status_code == 429:           # лимит МойСклад: запрос не выполнен, повтор безопасен и для записи
            wait = int(r.headers.get("X-Lognex-Retry-TimeInterval", 0) or 0) / 1000
            time.sleep(max(wait, 0.5 * (attempt + 1)))
            continue
        if r.status_code >= 500:
            if method != "GET":            # запись могла пройти — пусть вызывающий проверит (заказ ищется по externalCode)
                raise requests.HTTPError(f"МойСклад {r.status_code}", response=r)
            if attempt >= 2:
                break
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"МойСклад {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}
    raise RuntimeError("МойСклад не отвечает")


PAY_MESSAGE_DEFAULT = """💳 Реквизиты для оплаты

Kaspi.kz:
Переводом на номер:
+7 776 632 2485
Айша М.

После оплаты пришлите, пожалуйста, чек сюда"""


def pay_text():
    """Сообщение с реквизитами — бот шлёт его клиенту сразу после накладной. Текст можно заменить переменной PAY_MESSAGE."""
    try:
        import admin
        saved = admin.pay_text_saved()       # текст из панели управления
    except Exception:
        saved = ""
    return saved or os.environ.get("PAY_MESSAGE", "").replace("\\n", "\n").strip() or PAY_MESSAGE_DEFAULT


def meta(entity, eid):
    return {"meta": {"href": f"{API}/entity/{entity}/{eid}", "type": entity, "mediaType": "application/json"}}


def tg(method, **data):
    if not BOT:
        return None
    files = data.pop("_files", None)
    for _ in range(4):
        r = requests.post(f"https://api.telegram.org/bot{BOT}/{method}", data=data, files=files, timeout=30)
        j = r.json()
        if r.status_code == 429:           # Telegram просит подождать (много сообщений подряд)
            time.sleep(min(int((j.get("parameters") or {}).get("retry_after", 2)), 30) + 0.5)
            continue
        if not j.get("ok"):
            raise RuntimeError(f"Telegram {method}: {j.get('description', r.status_code)}")
        return j
    raise RuntimeError(f"Telegram {method}: лимит сообщений")


_notify_lock = threading.Lock()


def notify_bg(fn):
    """Уведомление владельцу в отдельном потоке; отправка по одной, чтобы при наплыве Telegram не отбрасывал сообщения."""
    def run():
        with _notify_lock:
            try:
                fn()
            except Exception as e:
                print("Уведомление:", e, flush=True)
            time.sleep(0.4)
    threading.Thread(target=run, daemon=True).start()


# ---------- оповещения владельцу ----------
_alerts = {}


def alert(key, text, every=600):
    """Пишет в лог и шлёт владельцу в Telegram, но не чаще раза в `every` секунд на один key."""
    print("ALERT", key, text, flush=True)
    now = time.time()
    if now - _alerts.get(key, 0) < every:
        return
    _alerts[key] = now
    try:
        tg("sendMessage", chat_id=OWNER, text="⚠️ Сайт AMURA: " + text)
    except Exception as e:
        print("Оповещение не отправлено:", e, flush=True)


# какие сообщения бот шлёт владельцу и сотрудникам (переключатели в панели: Обзор → Уведомления); ошибки сайта — всегда
NOTIF = {"msg": "Сообщения от клиентов",
         "handoff": "Клиент ждёт менеджера (передал ИИ), чеки и фото",
         "order": "Новый заказ с сайта (PDF накладной)",
         "invoice": "Клиент получил накладную",
         "newclient": "Новый клиент на сайте",
         "login": "Вход в панель",
         "review": "Новые отзывы о товарах"}


def notif_settings():
    def load():
        import inbox
        with inbox.db() as d:
            return {k: inbox.get_setting(d, "notif_" + k, "1") == "1" for k in NOTIF}
    try:
        return cached("notif", 30, load)
    except Exception as e:
        print("Настройки уведомлений:", e, flush=True)
        return {k: True for k in NOTIF}


def notif_on(key):
    return notif_settings().get(key, True)


def _is_staff(chat):
    if str(chat) in {str(OWNER)} | set(MANAGERS):
        return True
    try:
        import team
        return str(chat) in team.notify_chats() or bool(team.by_chat(chat))
    except Exception:
        return False


def notify_staff(key, text, every=0, method="sendMessage", **data):
    """Уведомление по клиентам: владельцу и всем менеджерам (MANAGER_CHAT_IDS). Не чаще раза в `every` секунд на key."""
    now = time.time()
    if every and now - _alerts.get(key, 0) < every:
        return
    _alerts[key] = now
    try:
        import team
        extra = team.notify_chats()
    except Exception:
        extra = []
    for c in dict.fromkeys([OWNER] + MANAGERS + extra):
        if not c:
            continue
        try:
            j = tg(method, chat_id=c, **({"text": text} if method == "sendMessage" else {"caption": text[:200]}), **data)
            mk = re.match(r"^(?:inbox|pdf|photo):(\d+)$", key)
            if mk and j:                                   # ответ на это уведомление уйдёт клиенту этого диалога
                import inbox
                inbox.map_notice(c, j["result"]["message_id"], int(mk.group(1)))
        except Exception as e:
            print("Уведомление не отправлено", c, e, flush=True)


# ---------- кэш каталога и справочников ----------
_cache = {}
_names = {}                   # id товара/услуги -> (название, код): из каталога и уже прочитанных заказов


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
    d = r.json()
    b = requests.get(f"{SITE_URL}/prices.bin", timeout=20, headers={"Cache-Control": "no-cache"})
    b.raise_for_status()
    opt = unseal(b.content, MS_TOKEN)        # id -> [опт, от 10 шт, короб, шт в коробе]
    _cache["site_updated"] = d.get("updated", "")
    out = []
    for i in d["items"]:
        o, m, bx, bq = opt.get(i["id"], (0, 0, 0, 0))
        out.append({"_site": True, **i, "rtl": i.get("rtl", 0), "opt": o, "mid": m, "box": bx, "boxQty": bq or i.get("boxQty", 0)})
    return out


def _delta():
    """Товары, изменённые в МойСклад после выгрузки catalog.json (цены, названия, новинки) — маленький быстрый запрос.
    Время в фильтре МойСклад — московское, выгрузка подписана временем Алматы (на 2 часа вперёд)."""
    try:
        base = datetime.strptime(_cache["site_updated"], "%d.%m.%Y %H:%M")
    except Exception:
        return []
    since = (base - timedelta(hours=2, minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for offset in range(0, 500, 100):
        d = ms("GET", "/entity/product", params={"filter": f"updated>{since}", "limit": 100, "offset": offset,
                                                   "order": "updated,asc"}, timeout=12)
        part = d.get("rows", [])
        rows += part
        if len(part) < 100:
            break
    print(f"Изменённых товаров после выгрузки: {len(rows)}", flush=True)
    return rows


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
    try:
        import admin
        hidden = admin.hidden_ids()          # товары, скрытые в панели управления
    except Exception:
        hidden = set()
    for r in products:
        _names[r["id"]] = (r.get("name", ""), r.get("code", ""))   # справочник названий для позиций заказов
        if r.get("_site"):                 # строка из catalog.json — цены уже готовы, обновляем только остаток
            qty = int(stock.get(r["id"], 0) or 0)
            if qty > 0:
                items.append({k: v for k, v in r.items() if k not in ("_site", "desc")} | {"qty": qty})
            descs[r["id"]] = r.get("desc", "")
            continue
        if r.get("code") == LOADER_CODE:
            continue
        qty = int(stock.get(r["id"], 0) or 0)
        opt, rtl = _price(r, PRICE_OPT), _price(r, PRICE_RTL)
        descs[r["id"]] = (r.get("description") or "")[:4000]
        if qty <= 0 or (opt <= 0 and rtl <= 0):
            continue
        mid, box = _price(r, PRICE_MID), _price(r, PRICE_BOX)
        bq = max([int(p.get("quantity", 0)) for p in (r.get("packs") or []) if p.get("quantity", 0) > 1] or [0])
        upd = r.get("updated", "")
        items.append({
            "id": r["id"], "name": r.get("name", ""), "brand": _brand(r), "group": (r.get("pathName") or "").strip(), "code": r.get("code", ""),
            "article": r.get("article", ""), "country": _country(r), "barcode": _barcode(r), "qty": qty,
            "opt": opt, "mid": mid if 0 < mid < opt else 0,
            "box": box if (0 < box < opt and bq) else 0, "boxQty": bq, "rtl": rtl,
            "img": f"img/{r['id']}.webp" if r["id"] in imgs else None,
            "updated": upd[:10], "isNew": upd[:10] >= new_since,
        })
    hid_items = [i for i in items if i["id"] in hidden]      # скрытые: клиентам не отдаются, видны только в панели
    items = [i for i in items if i["id"] not in hidden]
    _cache["descs"] = (time.time(), descs)
    # описание не входит в живой каталог (он уходит клиентам каждые 3 минуты) — отдаётся по /product/<id>
    return {"updated": datetime.now(ALMATY).strftime("%d.%m.%Y %H:%M"), "items": items, "hiddenItems": hid_items}


_live_lock = threading.Lock()
_last_hit = [0.0]


def refresh(max_age):
    """Обновляет кэш: карточки раз в 10 минут, остатки — когда данные старше max_age."""
    with _live_lock:
        v = _cache.get("live")
        if v and time.time() - v[0] < max_age:
            return v[1]
        products = cached("products", 600, _products)
        try:
            extra = cached("delta", 120, _delta)
        except Exception as e:             # не получилось — работаем по выгрузке, остатки всё равно живые
            print("Изменения товаров не получены:", e, flush=True)
            extra = []
        if extra:
            ids = {r["id"] for r in extra}
            products = [p for p in products if p["id"] not in ids] + extra
        data = build_live(products)
        _cache["live"] = (time.time(), data)
        return data


def _background():
    """Пока сайтом пользуются, держим каталог свежим в фоне — клиенты не ждут МойСклад."""
    fails, alerted = 0, False
    while True:
        time.sleep(20)
        if time.time() - _last_hit[0] > 1200:
            continue
        try:
            refresh(LIVE_TTL)
            if alerted:
                try:
                    tg("sendMessage", chat_id=OWNER, text="✅ Сайт AMURA: связь с МойСклад восстановилась")
                except Exception as e:
                    print("Оповещение не отправлено:", e, flush=True)
            fails, alerted = 0, False
        except Exception as e:
            fails += 1
            print("Фоновое обновление каталога:", e, flush=True)
            if fails >= 3:
                alerted = True
                alert("bg", f"каталог не обновляется из МойСклад ({fails} раз подряд): {str(e)[:300]}")


def _probe():
    time.sleep(3)
    try:
        t0 = time.time(); ms("GET", "/entity/organization", params={"limit": 1}, timeout=20)
        print(f"Проверка МойСклад: ответ за {time.time() - t0:.1f} с", flush=True)
        t0 = time.time(); st = _stock()
        print(f"Проверка остатков: {len(st)} позиций за {time.time() - t0:.1f} с", flush=True)
    except Exception as e:
        print("Проверка МойСклад не прошла:", e, flush=True)
        alert("probe", f"при запуске сервера МойСклад не ответил: {str(e)[:300]}")


threading.Thread(target=_background, daemon=True).start()
threading.Thread(target=_probe, daemon=True).start()


def live(max_age=LIVE_TTL):
    _last_hit[0] = time.time()
    v = _cache.get("live")
    if v and time.time() - v[0] < max_age:
        return v[1]
    if v and max_age >= LIVE_TTL:         # посетитель не ждёт МойСклад: отдаём последние данные, обновляем в фоне
        if not _live_lock.locked():
            threading.Thread(target=_refresh_quiet, args=(max_age,), daemon=True).start()
        return v[1]
    return refresh(max_age)


def _refresh_quiet(max_age):
    try:
        refresh(max_age)
    except Exception as e:
        print("Обновление каталога:", e, flush=True)


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


def is_wholesale(cid):
    """Оптовик = у контрагента тег «опт» или выбран тип цены «Оптовая цена». Проверка раз в 5 минут."""
    if PUBLIC_WHOLESALE:
        return True
    if not cid:
        return False
    def check():
        cp = ms("GET", f"/entity/counterparty/{cid}", params={"expand": "priceType"}, timeout=10)
        tags = {t.strip().lower() for t in cp.get("tags") or []}
        return WHOLESALE_TAG in tags or (cp.get("priceType") or {}).get("name") == PRICE_OPT
    try:
        return cached("ws:" + cid, 300, check)
    except Exception as e:
        print("Проверка оптовика не удалась:", e, flush=True)
        v = _cache.get("ws:" + cid)
        return v[1] if v else False


def view_items(items, wholesale):
    """Что видит клиент: оптовик — опт / от 10 шт / короб; остальные — только розничную цену."""
    if wholesale:
        return [{k: v for k, v in i.items() if k not in ("rtl", "group")} for i in items if i.get("opt", 0) > 0]
    return [{k: v for k, v in i.items() if k not in ("rtl", "group")} | {"opt": i["rtl"], "mid": 0, "box": 0}
            for i in items if i.get("rtl", 0) > 0]


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
    ws = False if request.args.get("mode") == "retail" else is_wholesale(session_cid())   # розничный сайт — всегда розница
    ck = ("catjson", ws)                     # JSON и gzip собираются раз на обновление каталога, а не на каждого посетителя
    hit = _cache.get(ck)
    if hit and hit[0] is data:
        body = hit[1]
    else:
        raw = json.dumps({"updated": data["updated"], "wholesale": ws, "items": view_items(data["items"], ws)},
                         ensure_ascii=False, separators=(",", ":")).encode()
        body = (raw, gzip.compress(raw, 6))
        _cache[ck] = (data, body)
    gz = "gzip" in request.headers.get("Accept-Encoding", "")
    resp = Response(body[1] if gz else body[0], mimetype="application/json")
    if gz:
        resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Cache-Control"] = "private, max-age=60"
    resp.headers["Vary"] = "Authorization, Accept-Encoding"
    return resp


def organization():
    return cached("org", 86400, lambda: ms("GET", "/entity/organization", params={"limit": 1})["rows"][0]["id"])


STORE_ID = os.environ.get("MS_STORE_ID", "64e7ab5b-168d-11f0-0a80-0db0000b91a2")   # «Основной склад»


def store_id():
    """Склад для новых заказов: без него сценарий МойСклад создаёт отгрузку черновиком (провести нельзя)."""
    if STORE_ID:
        return STORE_ID
    def find():
        rows = ms("GET", "/entity/store", params={"limit": 100})["rows"]
        live = [r for r in rows if not r.get("archived")]
        return next((r["id"] for r in live if r.get("name") == "Основной склад"), (live or rows)[0]["id"])
    return cached("store", 86400, find)


LOADER_ID = os.environ.get("LOADER_ID", "5c6dc5bf-3baa-11f0-0a80-031800089db8")      # «Услуга грузчика» (код 00308)
FEE_ID = os.environ.get("FEE_SERVICE_ID", "f5b6ae93-bd87-11f1-0a80-05d1002f5db2")   # услуга «Комиссия банка»


def loader_id():
    if LOADER_ID:                  # известный id — без медленного поиска в МойСклад
        return LOADER_ID
    return cached("loader", 86400, lambda: ms("GET", "/entity/product",
                                                params={"filter": f"code={LOADER_CODE}", "limit": 1})["rows"][0]["id"])


DELIVERY_NAME = "Доставка"


def delivery_service_id():
    """Услуга «Доставка» в МойСклад (для розничных заказов); нет — создаём."""
    def find_or_create():
        rows = ms("GET", "/entity/service", params={"filter": f"name={DELIVERY_NAME}", "limit": 1})["rows"]
        if rows:
            return rows[0]["id"]
        return ms("POST", "/entity/service", json={"name": DELIVERY_NAME})["id"]
    return cached("dlvsvc", 86400, find_or_create)


def fee_service_id():
    if FEE_ID:
        return FEE_ID
    def find_or_create():
        rows = ms("GET", "/entity/service", params={"filter": f"name={FEE_NAME}", "limit": 1})["rows"]
        if rows:
            return rows[0]["id"]
        return ms("POST", "/entity/service", json={"name": FEE_NAME})["id"]
    return cached("fee", 86400, find_or_create)


def unit_price(item, qty, wholesale=True):
    if not wholesale:
        return item.get("rtl", 0)
    if item.get("box") and item.get("boxQty") and qty >= item["boxQty"]:
        return item["box"]
    if item.get("mid") and qty >= TIER_MID:
        return item["mid"]
    return item["opt"]


_agents = {}


_retail_marked = set()


def mark_retail(cid):
    """Пометить покупателя меткой «розница» (один раз; повторно МойСклад не дёргаем)."""
    if not cid or cid in _retail_marked:
        return
    tags = ms("GET", f"/entity/counterparty/{cid}").get("tags") or []
    if RETAIL_TAG not in tags:
        ms("PUT", f"/entity/counterparty/{cid}", json={"tags": sorted(set(tags + [RETAIL_TAG]))})
    _retail_marked.add(cid)


def find_or_create_agent(name, phone, telegram, city):
    ck = phone[-10:] if phone else "@" + telegram.lower()
    hit = _agents.get(ck)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    aid = _find_or_create_agent(name, phone, telegram, city)
    _agents[ck] = (time.time(), aid)
    return aid


def _find_or_create_agent(name, phone, telegram, city):
    if phone:
        r = cp_by_phone(phone)
        if r:
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
    allowed = {SITE_URL.split("/amura-shop")[0], "https://amura.kz", "https://www.amura.kz", "https://admin.amura.kz"}
    if origin in allowed:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, PATCH, PUT, OPTIONS"
    return resp


# ---------- POST /order ----------
_orders = {}                  # ключ заказа -> (время, ответ): повтор не создаёт второй заказ
_orders_lock = threading.Lock()
_key_locks = {}               # ключ заказа -> [время, замок]


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
    with _orders_lock:
        now = time.time()
        for k in [k for k, v in _orders.items() if now - v[0] > 600]:
            del _orders[k]
        for k in [k for k, v in _key_locks.items() if now - v[0] > 600 and not v[1].locked()]:
            del _key_locks[k]
        lk = _key_locks.setdefault(key, [now, threading.Lock()])
        lk[0] = now
    with lk[1]:                                # разные заказы идут параллельно; повтор того же заказа ждёт и получает тот же ответ
        if key in _orders:
            return _orders[key][1]
        resp = _create_order_impl(key)
        if not isinstance(resp, tuple):        # ошибки (tuple) не кэшируем — повтор разрешён
            _orders[key] = (time.time(), resp)
        return resp


def _post_order(body):
    """Создаёт заказ один раз. Если ответ МойСклад не дошёл — ищем заказ по externalCode, а не создаём второй."""
    try:
        return ms("POST", "/entity/customerorder", json=body, timeout=10)
    except requests.RequestException as e:
        print("Заказ: ответ МойСклад не дошёл, ищу по externalCode:", e.__class__.__name__, flush=True)
    looked = False
    for _ in range(4):
        try:
            rows = ms("GET", "/entity/customerorder", params={"filter": f"externalCode={body['externalCode']}", "limit": 1},
                      timeout=8)["rows"]
            looked = True
            if rows:
                return rows[0]
        except Exception as e:
            print("Заказ: поиск по externalCode не удался:", e, flush=True)
        time.sleep(2)
    if looked:                                 # точно знаем, что заказа нет — создаём ещё раз
        return ms("POST", "/entity/customerorder", json=body, timeout=20)
    raise RuntimeError("МойСклад не подтвердил создание заказа")


def _create_order_impl(key):
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    payload, status = order_core(request.get_json(silent=True) or {}, key, ip, session_cid())
    payload.pop("_data", None)
    return jsonify(**payload) if status == 200 else (jsonify(**payload), status)


def order_core(d, key, ip, me, source="с сайта", panel=False):
    """Создание заказа в МойСклад (общее для сайта, ИИ-продажника и панели). Возвращает (словарь, http-статус).
    panel=True: заказ создаёт менеджер — без лимита частоты, оптовые цены, менеджер может задать свою цену."""
    if not panel and too_many(ip):
        return dict(ok=False, error="Слишком много заказов подряд, подождите 10 минут"), 429
    name = str(d.get("name", "")).strip()[:80]
    city = str(d.get("city", "")).strip()[:80]
    phone = re.sub(r"\D", "", str(d.get("phone", "")))[:15]
    if len(phone) == 11 and phone[0] == "8":
        phone = "7" + phone[1:]
    elif len(phone) == 10:
        phone = "7" + phone
    telegram = re.sub(r"[^A-Za-z0-9_]", "", str(d.get("telegram", "")))[:32]
    ship = d.get("shipping")
    retail_req = not panel and str(d.get("mode", "")) == "retail"
    if retail_req and ship not in RETAIL_SHIPPING:
        ship = None
    if retail_req and not me:                       # розница: заказ только после входа / регистрации
        return dict(ok=False, error="Войдите или зарегистрируйтесь, чтобы оформить заказ", login=True), 401
    if not name or not city or ship not in SHIPPING or not (len(phone) >= 10 or len(telegram) >= 4):
        return dict(ok=False, error="Заполните имя, контакт, город и способ отправки"), 400

    # Цены и остатки пересчитываются на сервере по МойСклад — цены из браузера не используются
    try:
        cat = catalog()
    except Exception as e:
        print("Заказ: МойСклад не ответил по остаткам:", e, flush=True)
        v = _cache.get("live")
        if not v:
            return dict(ok=False, error="Склад сейчас не отвечает, попробуйте через минуту"), 503
        cat = {i["id"]: i for i in v[1]["items"]}
    retail = retail_req                                             # заказ с розничного сайта (amura.kz/shop)
    if retail:
        source = RETAIL_SOURCE
    ws = True if panel else (False if retail else is_wholesale(me))
    lines = []
    for p in (d.get("items") or [])[:1000 if panel else 200]:
        item = cat.get(str(p.get("id")))
        qty = int(p.get("qty") or 0)
        if not item or qty <= 0:
            continue
        qty = min(qty, int(item["qty"]))
        price = unit_price(item, qty, ws)
        if panel and p.get("price") not in (None, ""):
            try:
                price = max(0, round(float(p["price"])))
            except (TypeError, ValueError):
                pass
        if qty <= 0 or price <= 0:
            continue
        lines.append({"id": item["id"], "name": item["name"], "qty": qty, "price": price})
    if not lines:
        return dict(ok=False, error="Корзина пуста или товаров нет в наличии"), 400

    ship_name, need_loader = SHIPPING[ship]
    logistics = str(d.get("logistics", "")).strip()[:80]
    recipient = str(d.get("recipient", "")).strip()[:100]
    zipcode = re.sub(r"\D", "", str(d.get("zip", "")))[:6]
    address = str(d.get("address", "")).strip()[:200]
    if need_loader and not logistics:
        return dict(ok=False, error="Укажите, через какую логистику отправить"), 400
    if ship == "kazpost" and not (recipient and len(zipcode) == 6 and address):
        return dict(ok=False, error="Для Казпочты укажите ФИО, индекс и адрес"), 400
    goods = sum(l["qty"] * l["price"] for l in lines)
    dlv = 0
    if retail:                                   # розница: цена доставки — по настройкам, считаем здесь (как на WB)
        import checkout
        dlv, ship_name, err = checkout.order_delivery(d, ship, city, goods)
        if err:
            return dict(ok=False, error=err), 400
    else:
        if ship == "courier" and not address:
            return dict(ok=False, error="Укажите адрес доставки по Алматы"), 400
        if ship == "cdek" and not address:
            return dict(ok=False, error="Укажите город и адрес пункта СДЭК"), 400
        if ship in ("courier", "cdek"):
            ship_name += f" — {address}"
    if need_loader:
        ship_name += f" — {logistics}"
    elif ship == "kazpost":
        ship_name += f" — {recipient}, {zipcode}, {address}"
    loader = LOADER_PRICE if need_loader else 0
    fee = 0 if retail else int((goods + loader) * FEE_RATE + 0.5)  # как Math.round на сайте; рознице комиссию не берём
    total = goods + loader + fee + dlv

    try:
        agent = me or find_or_create_agent(name, phone, telegram, city)
        positions = [{"quantity": l["qty"], "price": l["price"] * 100, "reserve": l["qty"],   # резерв товара под заказ
                      "assortment": meta("product", l["id"])} for l in lines]
        if loader:
            positions.append({"quantity": 1, "price": loader * 100, "assortment": meta("product", loader_id())})
        if fee:
            positions.append({"quantity": 1, "price": fee * 100, "assortment": meta("service", fee_service_id())})
        if dlv:
            positions.append({"quantity": 1, "price": dlv * 100, "assortment": meta("service", delivery_service_id())})
        contact = f"WhatsApp +{phone}" if phone else f"Telegram @{telegram}"
        order = _post_order({
            "externalCode": "site-" + hashlib.sha1(key.encode()).hexdigest()[:24],
            "organization": meta("organization", organization()),
            "store": meta("store", store_id()),          # склад сразу: отгрузка по сценарию проводится, а не остаётся черновиком
            "agent": meta("counterparty", agent),
            "shipmentAddress": city,
            "description": f"Заказ {source}\n{name}, {contact}\nГород: {city}\nОтправка: {ship_name}",
            "positions": positions,
        })
    except Exception as e:
        # МойСклад упал — заказ всё равно не теряем: шлём владельцу
        tg("sendMessage", chat_id=OWNER, text=f"⚠️ Заказ с сайта НЕ записан в МойСклад ({e})\n\n{json.dumps(d, ensure_ascii=False)[:3500]}")
        return dict(ok=False, error="Не удалось сохранить заказ, менеджер уже получил его и свяжется с вами"), 502

    number = order["name"]
    if retail:
        def _mark():
            try:
                mark_retail(agent)
            except Exception as e:
                print("Розница: метка покупателя не поставлена:", str(e)[:200], flush=True)
        notify_bg(_mark)
    tok = sign(number)
    data = {"number": number, "date": datetime.now(ALMATY).strftime("%d.%m.%Y %H:%M"), "name": name,
            "contact": contact, "city": city, "ship": ship_name, "lines": lines,
            "loader": loader, "fee": fee, "delivery": dlv, "total": total}
    def notify_owner():                        # PDF и Telegram — в фоне, клиент не ждёт
        try:
            pdf = build_pdf(data)
            pdf_store(number, pdf)             # накладная откроется мгновенно, без запроса в МойСклад
            caption = f"🛒 Заказ {source} № {number}\n{name} · {contact}\n{city} · {ship_name}"[:1000]   # состав и суммы — в PDF
            try:                                   # push в панель: звук и счётчик на иконке
                import push
                push.notify(f"🛒 Заказ № {number}", f"{name} · {fmt(total)} ₸ · {city}", "/admin#orders", perm="orders", tag=f"o{number}")
            except Exception as e:
                print("Push о заказе:", e, flush=True)
            try:                                   # номер последнего заказа — для счётчика новых заказов в панели
                import inbox
                with inbox.db() as d:
                    inbox.set_setting(d, "last_order", str(number))
            except Exception as e:
                print("Последний заказ не сохранён:", e, flush=True)
            if notif_on("order"):
                tg("sendDocument", chat_id=OWNER, caption=caption, _files={"document": (f"AMURA-{number}.pdf", pdf, "application/pdf")})
                print(f"Заказ {number}: уведомление владельцу отправлено", flush=True)
        except Exception as e:                 # заказ уже в МойСклад — сбой Telegram не должен ломать ответ клиенту
            print("Заказ", number, "Telegram не ответил:", e, flush=True)
            alert("notify", f"заказ № {number} записан в МойСклад, но PDF в Telegram не ушёл: {str(e)[:200]}")

    notify_bg(notify_owner)

    return dict(ok=True, number=number, total=total, startToken=f"{number}_{tok}",
                pdfUrl=f"{PUBLIC_URL}/order/{number}/pdf?t={tok}", _data=data), 200


# ---------- PDF ----------
def order_positions(oid):
    """Все позиции заказа. Без expand: с полными карточками товаров МойСклад на больших заказах не успевает ответить.
    Названия берём из справочника каталога, недостающие — короткими пачками."""
    rows, off = [], 0
    while True:
        part = ms("GET", f"/entity/customerorder/{oid}/positions", params={"limit": 1000, "offset": off}, timeout=25)["rows"]
        rows += part
        if len(part) < 1000 or off > 10000:
            break
        off += 1000
    need = {}
    for p in rows:
        a = p["assortment"]
        a["id"] = a["meta"]["href"].rstrip("/").rsplit("/", 1)[-1]
        if a["id"] not in _names:
            need.setdefault(a["meta"]["type"], []).append(a["id"])
    for typ, ids in need.items():
        ids = list(dict.fromkeys(ids))
        for k in range(0, len(ids), 40):
            part = ids[k:k + 40]
            try:
                for r in ms("GET", f"/entity/{typ}", params={"filter": ";".join(f"id={i}" for i in part), "limit": 100}, timeout=20)["rows"]:
                    _names[r["id"]] = (r.get("name", ""), r.get("code", ""))
            except Exception as e:
                print("Названия позиций пачкой не получены:", typ, e, flush=True)
            for i in part:
                if i not in _names:
                    try:
                        r = ms("GET", f"/entity/{typ}/{i}", timeout=15)
                        _names[i] = (r.get("name", ""), r.get("code", ""))
                    except Exception as e:
                        print("Название позиции не получено:", i, e, flush=True)
    for p in rows:
        a = p["assortment"]
        a["name"], a["code"] = _names.get(a["id"], ("Товар", ""))
    return rows


def order_from_ms(number, oid=None):
    if oid:                                    # id известен (из списка заказов) — без поиска по номеру, быстрее
        o = ms("GET", f"/entity/customerorder/{oid}", params={"expand": "agent"}, timeout=15)
        if o.get("name") != str(number):
            return None
    else:
        rows = ms("GET", "/entity/customerorder", params={"filter": f"name={number}", "limit": 1, "expand": "agent"}, timeout=15)["rows"]
        if not rows:
            return None
        o = rows[0]
    o["positions"] = {"rows": order_positions(o["id"])}
    desc = (o.get("description") or "").split("\n")
    lines, loader, fee = [], 0, 0
    for p in o["positions"]["rows"]:
        a, price, qty = p["assortment"], p["price"] / 100, p["quantity"]
        if a.get("code") == LOADER_CODE:
            loader += price * qty
        elif a["meta"]["type"] == "service" and a.get("name") == FEE_NAME:
            fee += price * qty
        else:
            lines.append({"id": a.get("id", ""), "name": a["name"], "code": a.get("code", ""), "qty": int(qty), "price": price})
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
    if o.get("fee"):
        rows.append(["", f"{FEE_NAME} 0,95%", "", "", fmt(o["fee"])])
    if o.get("delivery"):
        rows.append(["", DELIVERY_NAME, "", "", fmt(o["delivery"])])
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
    el += [t, Spacer(1, 6 * mm)]
    el += [Paragraph("Цены указаны в тенге. Менеджер свяжется с вами для подтверждения, оплаты и отправки.", n)]
    doc.build(el)
    return buf.getvalue()


_pdfs = {}                    # номер заказа -> PDF (последние 200)
_odata = {}                   # номер заказа -> данные, по которым собран PDF (для Excel без второго запроса в МойСклад)


def pdf_store(number, pdf, o=None):
    _pdfs[str(number)] = pdf
    if o:
        _odata[str(number)] = o
    while len(_pdfs) > 200:
        k = next(iter(_pdfs))
        _pdfs.pop(k)
        _odata.pop(k, None)


_thumbs = {}                   # id товара -> PNG-миниатюра для Excel (или b"" — фото нет)


def _thumb_png(pid):
    """Фото товара для Excel: webp с сайта → PNG 96×96 на белом фоне (Excel не показывает webp)."""
    if pid in _thumbs:
        return _thumbs[pid]
    data = b""
    try:
        path = os.path.join(_here, "img", f"{pid}.webp")
        if os.path.exists(path):
            with open(path, "rb") as f:
                raw = f.read()
        else:
            r = requests.get(f"{SITE_URL}/img/{pid}.webp", timeout=6)
            raw = r.content if r.ok else b""
        if raw:
            from PIL import Image
            im = Image.open(io.BytesIO(raw)).convert("RGBA")
            im.thumbnail((96, 96))
            bg = Image.new("RGB", (96, 96), "white")
            bg.paste(im, ((96 - im.width) // 2, (96 - im.height) // 2), im)
            out = io.BytesIO()
            bg.save(out, "PNG", optimize=True)
            data = out.getvalue()
    except Exception as e:
        print("Фото для Excel:", pid, e, flush=True)
    if len(_thumbs) > 2000:
        _thumbs.clear()
    _thumbs[pid] = data
    return data


def build_xlsx(o):
    from concurrent.futures import ThreadPoolExecutor
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XImage
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = f"Заказ {o['number']}"[:31]
    ws.append([f"Заказ покупателя № {o['number']} от {o['date']}"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Покупатель: {o['name']}, {o['contact']}"])
    ws.append([f"Город: {o['city']}    Отправка: {o['ship']}"])
    ws.append([])
    ws.append(["№", "Фото", "Код", "Товар", "Кол-во", "Цена, ₸", "Сумма, ₸"])
    for c in ws[5]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0E3B2C")
    ids = list({l.get("id") for l in o["lines"] if l.get("id")})
    with ThreadPoolExecutor(8) as ex:                    # фото подтягиваются параллельно
        pics = dict(zip(ids, ex.map(_thumb_png, ids)))
    for i, l in enumerate(o["lines"], 1):
        r = 5 + i
        ws.append([i, "", l.get("code", ""), l["name"], l["qty"], l["price"], f"=E{r}*F{r}"])
        png = pics.get(l.get("id"))
        if png:
            img = XImage(io.BytesIO(png))
            img.width = img.height = 60
            ws.add_image(img, f"B{r}")
        ws.row_dimensions[r].height = 48
    last = 5 + len(o["lines"])
    if o["loader"]:
        ws.append(["", "", "", "Услуга грузчика", 1, o["loader"], o["loader"]])
    if o.get("fee"):
        ws.append(["", "", "", f"{FEE_NAME} 0,95%", "", "", o["fee"]])
    ws.append(["", "", "", "Итого к оплате", "", "", o["total"]])
    ws.cell(ws.max_row, 4).font = ws.cell(ws.max_row, 7).font = Font(bold=True)
    for row in ws.iter_rows(min_row=6, max_row=ws.max_row, min_col=6, max_col=7):
        for c in row:
            c.number_format = "#,##0"
    for col, w in zip("ABCDEFG", (5, 10, 9, 56, 8, 11, 13)):
        ws.column_dimensions[col].width = w
    for r in range(6, last + 1):
        for c in range(1, 8):
            ws.cell(r, c).alignment = Alignment(wrap_text=(c == 4), vertical="center")
    ws.freeze_panes = "A6"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_pdf_busy = set()
_pdf_fail = {}                # номер -> время неудачи: не долбим МойСклад повторами
_pdf_ver = {}                 # номер заказа -> время изменения заказа в МойСклад, по которому собран PDF


def pdf_prepare(orders):
    """Заранее собрать PDF для заказов [(номер, id, время изменения)]: «PDF» в панели открывается сразу.
    Заказ изменили (в панели или прямо в МойСклад) — PDF пересобирается."""
    todo = []
    for n, i, *u in orders:
        n, upd = str(n), (u[0] if u else None)
        if n in _pdf_busy or (n in _pdfs and (upd is None or _pdf_ver.get(n) == upd)) or time.time() - _pdf_fail.get(n, 0) < 900:
            continue
        todo.append((n, i, upd))
    if not todo:
        return
    _pdf_busy.update(n for n, _, _ in todo)

    def run():
        for n, i, upd in todo:
            try:
                o = order_from_ms(n, i)
                if o:
                    pdf_store(n, build_pdf(o), o)
                    _pdf_ver[n] = upd
            except Exception as e:
                _pdf_fail[n] = time.time()
                print("PDF заранее не собран:", n, e, flush=True)
            finally:
                _pdf_busy.discard(n)
    threading.Thread(target=run, daemon=True).start()


@bp.route("/order/<number>/pdf")
def order_pdf(number):
    if not hmac.compare_digest(sign(number), request.args.get("t", "")):
        return "Ссылка недействительна", 403
    pdf = _pdfs.get(str(number))
    if pdf is None:
        try:
            o = order_from_ms(number)
        except Exception as e:                 # МойСклад завис — не пугаем клиента ошибкой и не шлём оповещение
            print("PDF заказа", number, "— МойСклад не ответил:", e, flush=True)
            return Response("МойСклад отвечает медленно. Обновите страницу через минуту.", 503, mimetype="text/plain; charset=utf-8",
                            headers={"Retry-After": "30"})
        if not o:
            return "Заказ не найден", 404
        pdf = build_pdf(o)
        pdf_store(number, pdf, o)
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="AMURA-{number}.pdf"'})


@bp.route("/order/<number>/xlsx")
def order_xlsx(number):
    if not hmac.compare_digest(sign(number), request.args.get("t", "")):
        return "Ссылка недействительна", 403
    o = _odata.get(str(number)) if str(number) in _pdfs else None
    if o is None:
        try:
            o = order_from_ms(number, request.args.get("id") if re.fullmatch(r"[0-9a-f-]{36}", request.args.get("id", "")) else None)
        except Exception as e:
            print("Excel заказа", number, "— МойСклад не ответил:", e, flush=True)
            return Response("МойСклад отвечает медленно. Попробуйте через минуту.", 503, mimetype="text/plain; charset=utf-8")
        if not o:
            return "Заказ не найден", 404
    return Response(build_xlsx(o), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="AMURA-{number}.xlsx"'})


# ---------- бот: /start <номер>_<подпись> ----------
@bp.route("/alert-test/<secret>")
def alert_test(secret):
    """Проверка оповещений: открыть ссылку — владельцу придёт тестовое сообщение."""
    if not HOOK_SECRET or not hmac.compare_digest(secret, HOOK_SECRET):
        return "", 403
    alert("test", "тест оповещений — всё работает", every=0)
    return jsonify(ok=True)


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
    if msg.get("reply_to_message") and _is_staff(chat):   # менеджер ответил на уведомление о клиенте — текст или голос клиенту
        try:
            import inbox
            if inbox.staff_reply(chat, msg):
                return "", 200
        except Exception as e:
            print("Ответ менеджера из Telegram:", e, flush=True)
    if text.strip().split("@")[0] == "/id":          # менеджер узнаёт свой chat_id, чтобы владелец добавил его в уведомления
        tg("sendMessage", chat_id=chat, text=f"Ваш chat_id: {chat}\nПередайте его владельцу, чтобы получать уведомления.")
        return "", 200
    sm = re.match(r"^/start\s+stf_([A-Za-z0-9]{10,40})$", text.strip())
    if sm:                                           # сотрудник открыл ссылку-приглашение из панели
        try:
            import team
            st = team.accept_invite(sm.group(1), chat, msg.get("from") or {})
        except Exception as e:
            print("Приглашение не принято:", e, flush=True)
            st = None
        if st:
            tg("sendMessage", chat_id=chat, text=f"{st['name']}, вы добавлены в команду AMURA ✅\nУведомления по клиентам будут приходить сюда. Вход в панель: страница /admin, введите свой @username и код из этого чата.")
            tg("sendMessage", chat_id=OWNER, text=f"✅ В команду добавлен: {st['name']}" + (f" (@{st['username']})" if st["username"] else ""))
        else:
            tg("sendMessage", chat_id=chat, text="Приглашение недействительно или устарело. Попросите владельца создать новое.")
        return "", 200
    am = re.match(r"^/start\s+adm_([A-Za-z0-9]{20,40})$", text.strip())
    if am:
        import admin
        admin.handle_admin_login(am.group(1), chat)
        return "", 200
    lm = re.match(r"^/start\s+login_([A-Za-z0-9]{20,40})$", text.strip())
    if lm:
        handle_tg_login(lm.group(1), msg.get("from") or {}, chat)
        return "", 200
    m = re.match(r"^/start\s+(\S+?)_([0-9a-f]{12})$", text.strip())
    if m and hmac.compare_digest(sign(m.group(1)), m.group(2)):
        o = order_from_ms(m.group(1))
        if o:
            sent = tg("sendDocument", chat_id=chat, caption=f"Ваш заказ AMURA № {o['number']} на {fmt(o['total'])} ₸.",
                      _files={"document": (f"AMURA-{o['number']}.pdf", build_pdf(o), "application/pdf")})
            tg("sendMessage", chat_id=chat, text=pay_text())   # реквизиты — только в мессенджер клиента, не на сайте и не в PDF
            try:                                   # клиент появляется в «Сообщениях» панели — можно сразу ему написать
                import inbox
                doc_id = ((sent or {}).get("result") or {}).get("document", {}).get("file_id", "")
                inbox.note_invoice(chat, msg.get("from") or {}, o["number"], o["total"], doc_id)
            except Exception as e:
                print("Накладная: диалог в панели не создан:", e, flush=True)
            user = (msg.get("from") or {}).get("username", "")
            if notif_on("invoice"):
                tg("sendMessage", chat_id=OWNER, text=f"Клиент @{user or chat} получил накладную по заказу № {o['number']}")
            return "", 200
    doc = msg.get("document") or {}
    media = msg.get("voice") or msg.get("audio") or msg.get("video_note")                # голосовые, аудио, «кружки»
    pdf = doc if (doc.get("mime_type") == "application/pdf" or str(doc.get("file_name", "")).lower().endswith(".pdf")) else None
    vid = msg.get("video")
    if msg.get("photo") or media or vid or doc or (text and not text.startswith("/")):
        try:                                       # обычное сообщение клиента — в инбокс (ИИ или менеджер)
            import inbox
            photo = msg["photo"][-1]["file_id"] if msg.get("photo") else (doc["file_id"] if doc.get("mime_type", "").startswith("image/") else None)
            voice = media["file_id"] if media else None
            att = None                                                                        # вложение для показа в панели
            if msg.get("voice") or msg.get("audio"):
                att = {"t": "voice" if msg.get("voice") else "audio", "id": media["file_id"], "dur": media.get("duration", 0)}
            elif vid or msg.get("video_note"):
                v = vid or msg["video_note"]
                att = {"t": "video", "id": v["file_id"], "dur": v.get("duration", 0)}
            elif doc and not str(doc.get("mime_type", "")).startswith("image/"):
                att = {"t": "doc", "id": doc["file_id"], "name": doc.get("file_name", "файл"), "size": doc.get("file_size", 0), "mime": doc.get("mime_type", "")}
            cap = text or msg.get("caption", "")
            if att and att["t"] == "doc" and not pdf:
                cap = (f"📎 Файл «{att['name']}»" + (f"\n{cap}" if cap else ""))
            elif att and att["t"] == "video":
                cap = "🎥 Видео" + (f"\n{cap}" if cap else "")
            threading.Thread(target=inbox.on_client_message, args=(chat, msg.get("from") or {}, cap, photo, voice, ({"id": pdf["file_id"], "name": pdf.get("file_name", "документ.pdf"), "size": pdf.get("file_size", 0)} if pdf else None), att), daemon=True).start()
            return "", 200
        except Exception as e:
            print("Инбокс недоступен:", e, flush=True)
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


# ---------- избранное клиента: поле «Сайт: избранное» в карточке контрагента МойСклад ----------
ATTR_FAV = "Сайт: избранное"
FAV_MAX = 300


def fav_attr():
    def load():
        rows = ms("GET", "/entity/counterparty/metadata/attributes").get("rows", [])
        for r in rows:
            if r["name"] == ATTR_FAV:
                return r
        return ms("POST", "/entity/counterparty/metadata/attributes", json={"name": ATTR_FAV, "type": "text", "required": False})
    return cached("favattr", 86400, load)


def _clean_ids(ids):
    out = []
    for i in ids or []:
        i = str(i)
        if re.fullmatch(r"[0-9a-f-]{36}", i) and i not in out:
            out.append(i)
    return out[:FAV_MAX]


@bp.route("/fav", methods=["GET", "PUT", "OPTIONS"])
def fav():
    if request.method == "OPTIONS":
        return "", 204
    cid = session_cid()
    if not cid:
        return jsonify(ok=False, error="Войдите заново"), 401
    meta_ = fav_attr()["meta"]
    if request.method == "PUT":
        ids = _clean_ids((request.get_json(silent=True) or {}).get("ids"))
        ms("PUT", f"/entity/counterparty/{cid}", json={"attributes": [{"meta": meta_, "value": " ".join(ids)}]})
        return jsonify(ok=True, ids=ids)
    cp = ms("GET", f"/entity/counterparty/{cid}")
    val = next((a.get("value") or "" for a in cp.get("attributes") or [] if a.get("name") == ATTR_FAV), "")
    return jsonify(ok=True, ids=_clean_ids(str(val).split()))


def norm_phone(v):
    d = re.sub(r"\D", "", str(v or ""))[:15]
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    elif len(d) == 10:
        d = "7" + d
    return d


def _phone_searches(p10):
    # МойСклад ищет подстрокой, а номер в карточке бывает записан как угодно:
    # «+7 (701) 234-56-78», «8 701 234 56 78», «87012345678» — пробуем разные куски
    return [p10, f"{p10[3:6]}-{p10[6:8]}-{p10[8:]}", f"{p10[3:6]} {p10[6:8]} {p10[8:]}", p10[3:]]


def _mirror_agent_ids(p10):
    """Клиенты с этим номером из копии базы (если она включена) — поиск по цифрам, без оглядки на формат."""
    try:
        import mirror
        if not mirror.enabled():
            return []
        with mirror.db() as d:
            rows = d.run("SELECT id, phone FROM ms_agent WHERE deleted=0 AND archived=0 AND phone LIKE %s",
                         ("%" + p10[-2:],), many=True)
        return [r[0] for r in rows if norm_phone(r[1])[-10:] == p10]
    except Exception:
        return []


def cp_by_phone(phone):
    """Ищет клиента по номеру, как бы номер ни был записан в МойСклад, — чтобы не плодить дубли."""
    p10 = norm_phone(phone)[-10:]
    if len(p10) < 10:
        return None
    for cid in _mirror_agent_ids(p10):
        try:
            r = ms("GET", f"/entity/counterparty/{cid}")
        except Exception:
            continue
        if not r.get("archived") and norm_phone(r.get("phone", ""))[-10:] == p10:
            return r
    seen = set()
    for q in _phone_searches(p10):
        rows = ms("GET", "/entity/counterparty", params={"search": q, "limit": 50})["rows"]
        hits = [r for r in rows if r["id"] not in seen and norm_phone(r.get("phone", ""))[-10:] == p10]
        if hits:
            hits.sort(key=lambda r: (bool(r.get("archived")), r.get("created") or ""))
            return hits[0]
        seen.update(r["id"] for r in rows)
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
        if not cur.get("actualAddress") and notif_on("newclient"):        # первое заполнение профиля — сообщаем владельцу
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
    try:                                             # отслеживание курьера Яндекса — только своих заказов
        import courier
        tr = courier.client_tracks(cid)
        for o in orders:
            if o["number"] in tr:
                o["track"] = tr[o["number"]]
    except Exception as e:
        print("Кабинет: отслеживание", str(e)[:200], flush=True)
    return jsonify(ok=True, profile=profile(cp), orders=orders)


@bp.route("/me/push", methods=["GET", "POST", "OPTIONS"])
def me_push():
    """GET — публичный ключ для подписки; POST {sub} — включить уведомления о доставке на этом устройстве."""
    if request.method == "OPTIONS":
        return "", 204
    import push
    if request.method == "GET":
        return jsonify(ok=True, key=push.keys()[1])
    cid = session_cid()
    if not cid:
        return jsonify(ok=False, error="Войдите заново"), 401
    try:
        push.subscribe_client((request.get_json(silent=True) or {}).get("sub") or {}, cid)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True)


@bp.route("/me/track", methods=["GET", "OPTIONS"])
def me_track():
    """Лёгкий опрос для сайта (раз в 1,5 мин, пока есть заказ в пути): статусы доставки своих заказов, без МойСклад."""
    if request.method == "OPTIONS":
        return "", 204
    cid = session_cid()
    if not cid:
        return jsonify(ok=False, error="Войдите заново"), 401
    import courier
    return jsonify(ok=True, tracks=courier.client_tracks(cid))
