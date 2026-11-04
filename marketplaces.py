"""Модуль «Каналы продаж»: система AMURA сама связывает маркетплейсы с МойСклад — без платных интеграций МойСклад.
Каналы: Ozon и Wildberries (схема FBS — товар на нашем складе). Следующим будет Kaspi магазин: новый канал — новый класс ниже.

Что делает по каждому каналу, у которого вписаны ключи (раз в 5 минут заказы, раз в 15 минут остатки):
- Заказы → МойСклад. Новое отправление (Ozon) / сборочное задание (WB) → заказ покупателя в МойСклад: первая строка комментария
  «Заказ с Ozon (FBS)» / «Заказ с WB (FBS)» (как «Заказ с розничного сайта» у сайта), номер отправления, контрагент «Ozon» / «Wildberries»,
  склад — как у заказов с сайта, резерв товара. externalCode <канал>-<номер>: повтор не создаёт второй заказ.
  Маркетплейс отменил — заказ в МойСклад «Отменен», резерв снят.
  Заказы, появившиеся ДО первого включения канала, не переносим (их уже могли записать вручную).
- Остатки МойСклад → маркетплейс. Доступный остаток (остаток − резерв, как на сайте) минус запас (по умолчанию 20 %, не меньше 1 шт)
  уходит на склад FBS: оптовик забрал последнее — маркетплейс не примет заказ, который нечем собрать.
- Товар маркетплейса ↔ товар МойСклад: по штрихкоду, иначе артикул продавца (Ozon offer_id, WB vendorCode) = код или артикул в МойСклад.

Ключи — только переменные окружения на Render:
  Ozon — OZON_CLIENT_ID и OZON_API_KEY (кабинет Ozon → Настройки → Seller API), склад FBS — OZON_WAREHOUSE_ID, если складов несколько;
  WB — WB_API_TOKEN (кабинет WB → Интеграции по API; категории «Маркетплейс» и «Контент»), склад — WB_WAREHOUSE_ID, если складов несколько.
Несколько магазинов (28.10.2026): переменные без суффикса — первый магазин; следующие — те же с суффиксом _2, _3…
  (WB_API_TOKEN_2, OZON_CLIENT_ID_2 + OZON_API_KEY_2, склады *_WAREHOUSE_ID_2). *_NAME[_N] — название магазина для пометки и экрана,
  *_ORG_ID[_N] — организация МойСклад, если магазин на другом ИП. У магазина свой ключ канала (ozon2, wb2 — в mp_order и externalCode),
  пометка «Заказ с WB (FBS) — <название>», контрагент, сверки и доля остатка (mp_<ключ>_share, по умолчанию 100 %): склад МойСклад общий.
Выключено по умолчанию: модуль «Каналы продаж» в «Настройках». Методы сверены 27.10.2026:
  Ozon /v4/posting/fbs/list (v3 отключён), /v2/products/stocks, /v3/product/list, /v3/product/info/list, /v2/warehouse/list;
  WB /api/v3/orders, /api/v3/orders/status, PUT /api/v3/stocks/{склад} (по chrtId), /api/v3/warehouses, /content/v2/get/cards/list.
Выплаты маркетплейсов — следующим шагом."""
import bg
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, jsonify, request

import inbox
import modules
import order_hook as oh

bp = Blueprint("marketplaces", __name__)
ORDERS_EVERY, STOCKS_EVERY, MAP_TTL = 300, 900, 3600
LOOKBACK_DAYS = 7
MAX_TRIES = 12                                          # час неудачных попыток — ждём, пока владелец свяжет товар
RESERVE_PCT_DEFAULT = 20
ALMATY = timezone(timedelta(hours=5))
_ready = [False]
_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {}


def _when(ts):
    """ISO-время маркетплейса → секунды (0, если не разобрали)."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _money(v):
    """Цена площадки в любом виде: число, строка «1290.0000», объект {price|amount|value} или {units, nanos} → float (0, если нет)."""
    if isinstance(v, dict):
        if "units" in v:
            return _money(v.get("units")) + _money(v.get("nanos")) / 1e9
        return next((m for m in (_money(v.get(k)) for k in ("price", "amount", "value")) if m), 0.0)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------- каналы ----------
class Channel:
    """Общий вид канала (один магазин). fetch → заказы {ext, label, status: live/cancel/wait, raw, at, cutoff, lines[{ref, offer, barcodes, name, qty, price, cur}]};
    products → {ref: {offer, barcodes, name, archived}}; push_stocks([{ref, offer, stock}]) → ошибки.
    n — номер магазина: 1 — переменные без суффикса, 2 и дальше — с суффиксом _n."""
    base = env_prefix = base_title = base_agent = emoji = ""
    KEYS = ()

    def __init__(self, n=1):
        self.n, self.sfx = n, "" if n == 1 else f"_{n}"
        self.key = self.base if n == 1 else f"{self.base}{n}"
        self.sent = {}                                   # ref → последний отправленный остаток
        self.sent_at = 0.0
        self.last = {"orders": 0.0, "stocks": 0.0}

    def env(self, name):
        return os.environ.get(f"{self.env_prefix}_{name}{self.sfx}", "").strip()

    def keys_set(self):
        return all(self.env(k) for k in self.KEYS)

    def env_names(self):
        return " и ".join(f"{self.env_prefix}_{k}{self.sfx}" for k in self.KEYS)

    @property
    def name(self):
        return self.env("NAME")

    @property
    def title(self):
        return f"{self.base_title} — {self.name}" if self.name else self.base_title if self.n == 1 else f"{self.base_title} {self.n}"

    @property
    def mark(self):
        tail = self.name or ("" if self.n == 1 else f"магазин {self.n}")
        return f"Заказ с {self.base_title} (FBS)" + (f" — {tail}" if tail else "")

    @property
    def agent_name(self):
        tail = self.name or ("" if self.n == 1 else f"магазин {self.n}")
        return f"{self.base_agent}" + (f" — {tail}" if tail else "") + " (маркетплейс)"

    def org(self):
        return self.env("ORG_ID") or oh.organization()


class Ozon(Channel):
    base, env_prefix, base_title, base_agent, emoji = "ozon", "OZON", "Ozon", "Ozon", "🟦"
    KEYS = ("CLIENT_ID", "API_KEY")
    CANCEL = ("cancelled", "not_accepted", "cancelled_from_split_pending")
    WAIT = ("awaiting_registration", "acceptance_in_progress", "awaiting_approve")   # Ozon ещё оформляет — заказ создадим позже

    def api(self):
        return os.environ.get("OZON_API", "https://api-seller.ozon.ru").rstrip("/")

    def call(self, path, body):
        r = requests.post(self.api() + path, json=body, timeout=30,
                          headers={"Client-Id": self.env("CLIENT_ID"), "Api-Key": self.env("API_KEY"), "Content-Type": "application/json"})
        j = _json(r)
        if r.status_code >= 400:
            raise ApiError(r.status_code, (str(j.get("message") or "") or f"HTTP {r.status_code}")[:300])
        return j

    @staticmethod
    def res(j):
        """v4 отдаёт данные сверху, часть примеров — внутри result: берём то, что есть."""
        return j["result"] if isinstance(j.get("result"), dict) else j

    def fetch(self, since, to):
        out, cursor = [], ""
        for _ in range(30):
            r = self.res(self.call("/v4/posting/fbs/list", {"filter": {"since": _iso(since), "to": _iso(to)}, "limit": 100,
                                                            "cursor": cursor, "sort_dir": "ASC",
                                                            "with": {"financial_data": True}}))
            for p in r.get("postings") or []:
                if p.get("posting_number"):
                    out.append(self.norm(p))
            cursor = r.get("cursor") or ""
            if not r.get("has_next") or not cursor:
                break
        return out

    SHIPPED = ("delivering", "driver_pickup", "delivered", "sent_by_seller")   # передан в доставку — отгрузка в МойСклад
    LABEL_FROM = ("awaiting_deliver",) + SHIPPED            # этикетка Ozon есть после сборки (awaiting_deliver)

    def label(self, ext):
        """Этикетка отправления для термопринтера (Ozon отдаёт PDF 75×120 мм). → (имя файла, байты)."""
        r = requests.post(self.api() + "/v2/posting/fbs/package-label", json={"posting_number": [ext]}, timeout=60,
                          headers={"Client-Id": self.env("CLIENT_ID"), "Api-Key": self.env("API_KEY"), "Content-Type": "application/json"})
        if r.status_code >= 400 or not r.content.startswith(b"%PDF"):
            j = _json(r)
            raise ApiError(r.status_code, str(j.get("message") or f"этикетка не готова (HTTP {r.status_code})")[:300])
        return f"Этикетка Ozon {ext}.pdf", r.content

    def norm(self, p):
        raw = str(p.get("status_alias") or p.get("status") or "")
        fin = {}
        for f in (p.get("financial_data") or {}).get("products") or []:       # факт Ozon: цена, комиссия, выплата (по sku / product_id)
            fin[str(f.get("product_id") or "")] = f
        lines = []
        for x in p.get("products") or []:
            f = fin.get(str(x.get("sku") or "")) or fin.get(str(x.get("product_id") or "")) or {}
            price = _money(x.get("price")) or _money(f.get("price")) or _money(f.get("client_price"))
            pc = x.get("price")
            cur = str(x.get("marketplace_seller_price_currency_code") or x.get("currency_code")
                      or (pc.get("currency_code") or pc.get("currency") if isinstance(pc, dict) else "") or f.get("currency_code") or "")
            com = f.get("commission")                    # v4: {"amount", "currency", "percent"}; раньше commission_amount числом
            com_cur = com.get("currency") if isinstance(com, dict) else ""
            lines.append({"ref": str(x.get("product_id") or ""), "offer": str(x.get("product_offer_id") or x.get("offer_id") or ""),
                          "barcodes": [], "name": str(x.get("product_name") or x.get("name") or ""), "qty": int(x.get("quantity") or 0),
                          "price": price, "cur": cur,
                          "fact": {"commission": abs(_money(f.get("commission_amount")) or _money(com)), "payout": _money(f.get("payout")),
                                   "cur": str(com_cur or f.get("currency_code") or cur)} if f else {}})
        st = str(p.get("status") or "")
        return {"ext": str(p["posting_number"]), "label": f"Отправление {p['posting_number']}" + (f", заказ Ozon {p['order_number']}" if p.get("order_number") else ""),
                "shipped": raw in self.SHIPPED or st in self.SHIPPED, "label": raw in self.LABEL_FROM or st in self.LABEL_FROM,
                "status": "cancel" if raw in self.CANCEL else "wait" if raw in self.WAIT else "live", "raw": raw,
                "at": _when(p.get("in_process_at")), "cutoff": _when(p.get("shipment_date") or p.get("cutoff") or ""), "lines": lines}

    def products(self):
        ids, last = [], ""
        for _ in range(50):
            r = self.res(self.call("/v3/product/list", {"filter": {"visibility": "ALL"}, "last_id": last, "limit": 1000}))
            items = r.get("items") or []
            ids += [str(i["product_id"]) for i in items if i.get("product_id")]
            last = r.get("last_id") or ""
            if len(items) < 1000 or not last:
                break
        out = {}
        for k in range(0, len(ids), 1000):
            r = self.call("/v3/product/info/list", {"product_id": ids[k:k + 1000]})
            for i in r.get("items") or self.res(r).get("items") or []:
                out[str(i.get("id"))] = {"offer": str(i.get("offer_id") or ""), "barcodes": [str(b) for b in i.get("barcodes") or [] if b],
                                         "name": str(i.get("name") or ""), "archived": bool(i.get("is_archived")),
                                         "skus": [str(x["sku"]) for x in i.get("sources") or [] if x.get("sku")] + ([str(i["sku"])] if i.get("sku") else [])}
        return out

    def warehouse(self):
        w = self.env("WAREHOUSE_ID")
        if w:
            return int(w)

        def find():
            r = self.res(self.call("/v2/warehouse/list", {"limit": 100}))
            rows = [x for x in r.get("warehouses") or [] if not x.get("is_rfbs")]
            live = [x for x in rows if str(x.get("status") or "").lower() not in ("disabled", "archived", "blocked")]
            ids = [int(x.get("warehouse_id") or x.get("id")) for x in live if x.get("warehouse_id") or x.get("id")]
            if len(ids) != 1:
                raise ValueError("в кабинете Ozon " + ("нет склада FBS" if not ids else f"{len(ids)} склада FBS — впишите OZON_WAREHOUSE_ID{self.sfx} на Render"))
            return ids[0]
        return oh.cached(f"mp_wh_{self.key}", 3600, find)

    def push_stocks(self, rows):
        wh, errors = self.warehouse(), []
        for k in range(0, len(rows), 100):
            part = rows[k:k + 100]
            res = self.call("/v2/products/stocks", {"stocks": [{"offer_id": s["offer"], "product_id": int(s["ref"]), "stock": s["stock"],
                                                                "warehouse_id": wh} for s in part]})
            by_offer = {s["offer"]: s for s in part}
            for r in res.get("result") or []:
                s = by_offer.get(str(r.get("offer_id")))
                if r.get("updated") and s:
                    self.sent[s["ref"]] = s["stock"]
                elif not r.get("updated"):
                    errors.append(f"{r.get('offer_id')}: " + "; ".join(str(e.get("message") or e.get("code") or e) for e in r.get("errors") or []))
            time.sleep(0.5)
        return errors


class WB(Channel):
    base, env_prefix, base_title, base_agent, emoji = "wb", "WB", "WB", "Wildberries", "🟪"
    KEYS = ("API_TOKEN",)
    CANCEL_WB = ("canceled", "canceled_by_client", "declined_by_client", "defect")
    KZT = 398
    SHIPPED_WB = ("sorted", "sold", "ready_for_pickup", "postponed_delivery")   # с supplierStatus complete — передан в доставку
    ISO = {398: "KZT", 643: "RUB", 933: "BYN", 417: "KGS", 860: "UZS", 51: "AMD"}

    def label(self, ext):
        """Стикер сборочного задания 58×40 мм (WB отдаёт png/svg/zpl, PDF нет — берём png для печати с телефона). → (имя, байты)."""
        import base64
        j = self.req("POST", "marketplace", "/api/v3/orders/stickers", params={"type": "png", "width": 58, "height": 40},
                     json={"orders": [int(ext)]})
        st = next((x for x in j.get("stickers") or [] if str(x.get("orderId")) == str(ext) and x.get("file")), None)
        if not st:
            raise ApiError(404, "стикер не готов: задание ещё не в поставке")
        return f"Стикер WB {ext}.png", base64.b64decode(st["file"])

    @classmethod
    def cur(cls, code):
        """WB отдаёт валюту числом ISO 4217 (643 — рубли): переводим в буквенный код для курса."""
        try:
            return cls.ISO.get(int(code), str(code or ""))
        except (TypeError, ValueError):
            return str(code or "")

    def req(self, method, base, path, **kw):
        url = os.environ.get(f"WB_{base.upper()}_API", f"https://{base}-api.wildberries.ru").rstrip("/") + path
        if kw.get("params"):                             # requests шлёт False как «False», а WB ждёт «false» — иначе HTTP 400
            kw["params"] = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in kw["params"].items()}
        r = requests.request(method, url, timeout=30, headers={"Authorization": self.env("API_TOKEN"),
                                                              "Content-Type": "application/json"}, **kw)
        j = _json(r)
        if r.status_code >= 400:
            e = j[0] if isinstance(j, list) and j else j if isinstance(j, dict) else {}
            msg = " — ".join(str(e[k]) for k in ("message", "errorText", "title", "detail") if e.get(k)) or (r.text or "").strip()[:200]
            extra = {k: e[k] for k in ("additionalErrors", "errors", "data", "origin", "requestId") if e.get(k)}   # «bad request» без подробностей
            if extra:
                msg += " " + json.dumps(extra, ensure_ascii=False)[:300]
            raise ApiError(r.status_code, f"HTTP {r.status_code}: {msg}"[:400] if msg else f"HTTP {r.status_code}")
        return j

    def fetch(self, since, to):
        orders, nxt = [], 0
        for _ in range(30):
            j = self.req("GET", "marketplace", "/api/v3/orders", params={"limit": 1000, "next": nxt, "dateFrom": int(since), "dateTo": int(to)})
            part = j.get("orders") or []
            orders += part
            nxt = j.get("next") or 0
            if len(part) < 1000 or not nxt:
                break
        st = {}
        ids = [o["id"] for o in orders if o.get("id")]
        for k in range(0, len(ids), 1000):
            for x in (self.req("POST", "marketplace", "/api/v3/orders/status", json={"orders": ids[k:k + 1000]}).get("orders") or []):
                st[x.get("id")] = x
        return [self.norm(o, st.get(o["id"]) or {}) for o in orders if o.get("id")]

    def norm(self, o, st):
        sup, wbs = str(st.get("supplierStatus") or ""), str(st.get("wbStatus") or "")
        cancel = sup == "cancel" or wbs in self.CANCEL_WB
        if o.get("currencyCode") == self.KZT or not o.get("convertedPrice"):
            price, cur = _money(o.get("price")) / 100, "KZT" if o.get("currencyCode") == self.KZT else self.cur(o.get("currencyCode"))
        else:
            price = _money(o.get("convertedPrice")) / 100
            cur = "KZT" if o.get("convertedCurrencyCode") == self.KZT else self.cur(o.get("convertedCurrencyCode"))
        if not price:                                    # запасные поля цены в новых ответах WB (копейки)
            price = next((_money(o.get(k)) / 100 for k in ("finalPrice", "salePrice") if _money(o.get(k))), 0.0)
        return {"shipped": sup == "complete" or wbs in self.SHIPPED_WB, "label": sup == "confirm" and not cancel,
                "ext": str(o["id"]), "label": f"Сборочное задание {o['id']}" + (f", заказ WB {o['orderUid']}" if o.get("orderUid") else ""),
                "status": "cancel" if cancel else "wait" if not st else "live", "raw": f"{sup}/{wbs}" if st else "",
                "at": _when(o.get("createdAt")), "cutoff": 0,
                "lines": [{"ref": str(o.get("chrtId") or ""), "offer": str(o.get("article") or ""), "barcodes": [str(s) for s in o.get("skus") or []],
                           "name": str(o.get("article") or ""), "qty": 1, "price": price, "cur": cur}]}

    def products(self):
        out, cursor = {}, {"limit": 100}
        for _ in range(200):
            j = self.req("POST", "content", "/content/v2/get/cards/list", json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
            cards = j.get("cards") or []
            for c in cards:
                for s in c.get("sizes") or []:
                    if s.get("chrtID"):
                        out[str(s["chrtID"])] = {"offer": str(c.get("vendorCode") or ""), "barcodes": [str(b) for b in s.get("skus") or [] if b],
                                                 "name": str(c.get("title") or c.get("vendorCode") or ""), "archived": False,
                                                 "nm": c.get("nmID"), "subject": c.get("subjectID"), "dims": c.get("dimensions") or {},
                                                 "kiz": bool(c.get("needKiz"))}
            cur = j.get("cursor") or {}
            if len(cards) < 100 or not cur.get("nmID"):
                break
            cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
        return out

    def warehouse(self):
        w = self.env("WAREHOUSE_ID")
        if w:
            return int(w)

        def find():
            rows = [x for x in self.req("GET", "marketplace", "/api/v3/warehouses") or [] if not x.get("isDeleting")]
            if len(rows) != 1:
                raise ValueError("в кабинете WB " + ("нет своего склада" if not rows else f"{len(rows)} склада — впишите WB_WAREHOUSE_ID{self.sfx} на Render"))
            return int(rows[0]["id"])
        return oh.cached(f"mp_wh_{self.key}", 3600, find)

    def push_stocks(self, rows):
        wh, errors = self.warehouse(), []
        for k in range(0, len(rows), 1000):
            part = rows[k:k + 1000]
            try:
                self.req("PUT", "marketplace", f"/api/v3/stocks/{wh}",
                         json={"stocks": [{"chrtId": int(s["ref"]), "amount": s["stock"]} for s in part]})
                for s in part:
                    self.sent[s["ref"]] = s["stock"]
            except ApiError as e:
                errors.append(e.message)
            time.sleep(0.3)
        return errors


CHANNELS = {}                                           # ключ → магазин; состав — по переменным на Render (channels())


def channels():
    """Магазины: первый Ozon и первый WB есть всегда (работают, если вписан ключ), следующие — по переменным с суффиксом _2, _3…
    Объекты магазинов живут всё время работы — в них последние отправленные остатки и время сверок."""
    want = []
    for cls in (Ozon, WB):
        nums = {1} | {int(k.rsplit("_", 1)[1]) for k in os.environ
                      if k.startswith(f"{cls.env_prefix}_{cls.KEYS[0]}_") and k.rsplit("_", 1)[1].isdigit() and int(k.rsplit("_", 1)[1]) >= 2}
        want += [(cls, n) for n in sorted(nums)]
    keep = dict(CHANNELS)
    CHANNELS.clear()
    for cls, n in want:
        key = cls.base if n == 1 else f"{cls.base}{n}"
        CHANNELS[key] = keep.get(key) or cls(n)
    return list(CHANNELS.values())


channels()


def on(ch=None):
    try:
        if not modules.enabled("marketplaces"):
            return False
    except Exception:
        return False
    return ch.keys_set() if ch else any(c.keys_set() for c in channels())


# ---------- база и настройки ----------
COLS = "posting, status, order_id, number, total, error, tries, created, updated"


def db():
    d = inbox.db()
    if not _ready[0]:
        d.run("CREATE TABLE IF NOT EXISTS mp_order (mp TEXT, posting TEXT, status TEXT, order_id TEXT DEFAULT '', number TEXT DEFAULT '',"
              " total DOUBLE PRECISION DEFAULT 0, error TEXT DEFAULT '', tries INTEGER DEFAULT 0, created DOUBLE PRECISION,"
              " updated DOUBLE PRECISION, PRIMARY KEY (mp, posting))")
        d.c.commit()
        _ready[0] = True
    return d


def get(ch, ext):
    with db() as d:
        r = d.run(f"SELECT {COLS} FROM mp_order WHERE mp=%s AND posting=%s", (ch.key, ext), one=True)
    return dict(zip(COLS.split(", "), r)) if r else None


def _put(ch, ext, **f):
    now = time.time()
    with db() as d:
        if d.run("SELECT 1 FROM mp_order WHERE mp=%s AND posting=%s", (ch.key, ext), one=True):
            d.run(f"UPDATE mp_order SET {', '.join(k + '=%s' for k in f)}, updated=%s WHERE mp=%s AND posting=%s",
                  (*f.values(), now, ch.key, ext))
        else:
            f = {"status": "", "order_id": "", "number": "", "total": 0, "error": "", "tries": 0} | f
            d.run("INSERT INTO mp_order (mp, posting, status, order_id, number, total, error, tries, created, updated) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                  (ch.key, ext, f["status"], f["order_id"], f["number"], f["total"], f["error"], f["tries"], now, now))


def setting(key, default=""):
    with inbox.db() as d:
        return inbox.get_setting(d, key, default)


def set_setting(key, value):
    with inbox.db() as d:
        inbox.set_setting(d, key, str(value))


def since_mark(ch):
    """Время первого включения канала: заказы старше него в МойСклад не переносим."""
    v = setting(f"mp_{ch.key}_since", "")
    if not v:
        v = str(int(time.time()))
        set_setting(f"mp_{ch.key}_since", v)
    return float(v)


def reserve_pct():
    try:
        return max(0, min(90, int(setting("mp_reserve", str(RESERVE_PCT_DEFAULT)))))
    except ValueError:
        return RESERVE_PCT_DEFAULT


def share_pct(ch):
    """Доля остатка для магазина, %: склад МойСклад общий — у двух магазинов по 100 % одну штуку могут купить дважды."""
    try:
        return max(0, min(100, int(setting(f"mp_{ch.key}_share", "100"))))
    except ValueError:
        return 100


def shown(qty, pct=None, share=100):
    """Сколько показать на маркетплейсе: доступно минус запас (pct %, не меньше 1 шт; при 0 % — без запаса), из этого — доля магазина."""
    q = max(0, int(qty or 0))
    pct = reserve_pct() if pct is None else pct
    if q and pct:
        q = max(0, q - max(1, math.ceil(q * pct / 100)))
    return q * share // 100


# ---------- товары: маркетплейс ↔ МойСклад ----------
def _catalog():
    """Товары МойСклад, как в системе AMURA: id → {code, article, barcode, qty (доступно), name}."""
    return oh.panel_catalog()


def ms_by_codes(codes):
    """Товары МойСклад по коду или артикулу — для тех, кого нет в каталоге системы (в выгрузку попадают только товары
    в наличии: новинка без остатка карточку WB/Ozon иначе не находит). → {код в нижнем регистре: id}. Пачками по 40."""
    codes = sorted({str(c).strip() for c in codes if str(c or "").strip()})[:400]
    out = {}
    for k in range(0, len(codes), 40):
        part = codes[k:k + 40]
        for field in ("code", "article"):
            try:
                rows = oh.ms("GET", "/entity/product", params={"filter": ";".join(f"{field}={c}" for c in part), "limit": 100},
                             timeout=12).get("rows") or []
            except Exception as e:
                print("Товары МойСклад по коду:", str(e)[:200], flush=True)
                return out
            for r in rows:
                if r.get("archived"):
                    continue
                for c in (r.get("code"), r.get("article")):
                    if c:
                        out.setdefault(str(c).strip().lower(), r["id"])
    return out


def mapping(ch):
    """ref товара маркетплейса → id товара МойСклад (+ по артикулу продавца). Не нашли — «без пары»."""
    def build():
        cat = _catalog()
        by_bar, by_code = {}, {}
        for i in cat.values():
            if i.get("barcode"):
                by_bar[str(i["barcode"])] = i["id"]
            for c in (i.get("code"), i.get("article")):
                if c:
                    by_code[str(c).strip().lower()] = i["id"]
        prods = ch.products()
        ref2ms, offer2ms, lost = {}, {}, []
        for ref, x in prods.items():
            mid = next((by_bar[b] for b in x["barcodes"] if b in by_bar), None) or by_code.get(x["offer"].strip().lower())
            if mid:
                ref2ms[ref] = mid
                offer2ms[x["offer"]] = mid
            elif not x["archived"]:
                lost.append({"offer": x["offer"], "name": x["name"], "ref": ref})
        if lost:                                       # новинки без остатка — ищем в МойСклад по артикулу продавца
            extra = ms_by_codes(l["offer"] for l in lost)
            for l in list(lost):
                mid = extra.get(l["offer"].strip().lower())
                if mid:
                    ref2ms[l["ref"]] = mid
                    offer2ms[l["offer"]] = mid
                    by_code.setdefault(l["offer"].strip().lower(), mid)
                    lost.remove(l)
        for l in lost:
            l.pop("ref", None)
        return {"ref": ref2ms, "offer": offer2ms, "products": prods, "lost": lost, "by_bar": by_bar, "by_code": by_code}
    return oh.cached(f"mp_map_{ch.key}", MAP_TTL, build)


def ms_id_for(line, mp):
    return (mp["ref"].get(line["ref"]) or mp["offer"].get(line["offer"])
            or next((mp["by_bar"][b] for b in line.get("barcodes") or [] if b in mp["by_bar"]), None)
            or mp["by_code"].get(line["offer"].strip().lower()))


# ---------- заказы: маркетплейс → МойСклад ----------
def agent(ch):
    """Контрагент канала в МойСклад: находим по названию, нет — создаём один раз (id помним в настройках)."""
    aid = setting(f"mp_{ch.key}_agent", "")
    if aid:
        return aid
    names = {ch.agent_name.lower(), ch.agent_name.split(" (")[0].lower()}
    rows = oh.ms("GET", "/entity/counterparty", params={"search": ch.agent_name.split(" (")[0], "limit": 20}).get("rows") or []
    hit = next((r for r in rows if r.get("name", "").strip().lower() in names), None)
    aid = hit["id"] if hit else oh.ms("POST", "/entity/counterparty", json={
        "name": ch.agent_name, "companyType": "legal", "tags": ["маркетплейс"],
        "description": f"Покупатели {ch.title} (FBS). Заказы создаёт система AMURA"})["id"]
    set_setting(f"mp_{ch.key}_agent", aid)
    return aid


def _state(name):
    md = oh.cached("ms_order_states", 3600, lambda: oh.ms("GET", "/entity/customerorder/metadata", timeout=15))
    st = next((x for x in md.get("states", []) if x.get("name") == name), None)
    return st and st["meta"]


def _fail(ch, o, why, alert_text):
    rec = get(ch, o["ext"]) or {"tries": 0}
    _put(ch, o["ext"], status=o["raw"], error=why[:300], tries=rec["tries"] + 1)
    if rec["tries"] == 0:
        oh.alert(f"mp:{ch.key}:{o['ext']}", alert_text, every=3600)


def _fmt(v):
    return f"{round(v):,}".replace(",", " ")


def kzt_lines(ch, lines):
    """Цена продажи покупателю в тенге по каждой строке (рубли и др. — по курсу покупки МиГ, как в калькуляторе).
    → [(тенге за штуку, пометка курса)]; не перевели — (0, причина)."""
    import mp_calc
    out = []
    for l in lines:
        if not l["price"]:
            out.append((0.0, f"{ch.title} не передал цену"))
            continue
        v, note = mp_calc.to_kzt(l["price"], l["cur"])
        out.append((round(v), f"{l['price']:g} {l['cur']}, {note}") if v is not None else (0.0, note))
    return out


ECON = [("commission", "Комиссия"), ("logistics", "Логистика и последняя миля"), ("processing", "Приёмка и обработка FBS"),
        ("acquiring", "Эквайринг")]
OWN = [("pack", "Упаковка"), ("tax", "Налог ИП"), ("ads", "Реклама"), ("cost", "Себестоимость")]


def economy(ch, lines, mids, kzt):
    """Экономика заказа для комментария МойСклад: факт площадки, где он есть (комиссия Ozon), остальное — расчёт калькулятора
    с пометкой «оценка». Ошибка расчёта — короткая строка, заказ создаётся всё равно."""
    import mp_calc
    st, tot, notes, price, fact = mp_calc.settings(), {}, {}, 0.0, False
    for l, mid, (p, _) in zip(lines, mids, kzt):
        if not p:
            return "Экономика заказа: нет цены продажи — не посчитана"
        q = l["qty"] or 1
        price += p * q
        c = mp_calc.channel_calc(ch, mid, mp_calc.product(mid), st, price_override=p)
        if c.get("error"):
            return f"Экономика заказа: не посчитана — {c['error'][:200]}"
        for x in c["lines"]:
            tot[x["key"]] = tot.get(x["key"], 0) + (x["sum"] or 0) * q
            notes.setdefault(x["key"], set()).add("нет данных" if x["sum"] is None else "оценка" if x["key"] in dict(ECON) else
                                                 (x.get("note") or "")[:80])
        f = l.get("fact") or {}
        if f.get("commission"):                          # комиссия по данным площадки за всю строку
            v, _ = mp_calc.to_kzt(f["commission"], f.get("cur") or l["cur"])
            if v is not None:
                tot["commission"] += v - (next(x["sum"] for x in c["lines"] if x["key"] == "commission") or 0) * q
                notes["commission"] = {f"факт {ch.title}"}
                fact = True
    out = [f"Экономика заказа, ₸ ({'факт ' + ch.title + ' и ' if fact else ''}оценка калькулятора):", f"Продажа: {_fmt(price)}"]
    for k, label in ECON:
        out.append(f"{label}: −{_fmt(tot.get(k, 0))} ({', '.join(sorted(x for x in notes.get(k, ()) if x))})")
    payout = price - sum(tot.get(k, 0) for k, _ in ECON)
    out.append(f"К выплате: {_fmt(payout)} (оценка)")
    for k, label in OWN:
        if tot.get(k) or k in ("pack", "cost"):
            n = ", ".join(sorted(x for x in notes.get(k, ()) if x))
            out.append(f"{label}: −{_fmt(tot.get(k, 0))}" + (f" ({n})" if n else ""))
    profit = payout - sum(tot.get(k, 0) for k, _ in OWN)
    out.append(f"Прибыль: {_fmt(profit)}, маржа {round(profit / price * 100, 1):g} %".replace(".", ","))
    return "\n".join(out)


def create_order(ch, o, mp):
    """Заказ маркетплейса → заказ МойСклад. Все товары должны найтись, иначе заказ не создаём (запись с ошибкой, тревога)."""
    lines = o["lines"]
    miss = [l for l in lines if not ms_id_for(l, mp)]
    if miss or not lines:
        if f"mp_map_{ch.key}" in oh._cache:              # товар могли только что добавить — к следующей сверке список обновится
            oh._cache[f"mp_map_{ch.key}"] = (0, oh._cache[f"mp_map_{ch.key}"][1])
        why = ("нет пары в МойСклад: " + ", ".join(f"{l['offer'] or l['ref']} {l['name'][:40]}" for l in miss)) if miss else "в заказе нет товаров"
        _fail(ch, o, why, f"{ch.title}: заказ {o['ext']} не записан в МойСклад — {why[:300]}. "
                          f"Свяжите товар: одинаковый штрихкод или артикул {ch.title} = код товара в МойСклад")
        return None
    cat, positions, total = _catalog(), [], 0
    mids, kzt = [ms_id_for(l, mp) for l in lines], kzt_lines(ch, lines)
    for l, mid, (p, _) in zip(lines, mids, kzt):
        free = int((cat.get(mid) or {}).get("qty") or 0)
        positions.append({"quantity": l["qty"], "price": round(p * 100), "reserve": min(l["qty"], max(free, 0)),
                          "assortment": oh.meta("product", mid)})
        total += p * l["qty"]
    till = datetime.fromtimestamp(o["cutoff"], ALMATY).strftime("%d.%m %H:%M") if o["cutoff"] else ""
    desc = (f"{ch.mark}\n{o['label']}" + (f"\nСобрать и сдать до: {till}" if till else "") + _rate_note(lines, kzt)
            + "\n\n" + _economy_safe(ch, lines, mids, kzt))
    if not total:
        oh.alert(f"mp-zero:{ch.key}:{o['ext']}", f"{ch.title}: заказ {o['ext']} пришёл без цены — в МойСклад цена 0, проверьте", every=86400)
    try:
        order = oh._post_order({"externalCode": f"{ch.key}-{o['ext']}", "organization": oh.meta("organization", ch.org()),
                                "store": oh.meta("store", oh.store_id()), "agent": oh.meta("counterparty", agent(ch)),
                                "description": desc, "positions": positions})
    except Exception as e:
        _fail(ch, o, f"МойСклад: {str(e)[:250]}", f"{ch.title}: заказ {o['ext']} не записан в МойСклад ({str(e)[:200]}). Повторю через 5 минут")
        return None
    _put(ch, o["ext"], status=o["raw"], order_id=order.get("id", ""), number=str(order.get("name", "")), total=round(total), error="")
    try:
        oh.notify_staff("orders", f"{ch.emoji} Заказ с {ch.title} № {order.get('name', '')}: {sum(l['qty'] for l in lines)} шт "
                                  f"на {oh.fmt(round(total))} ₸\n{o['label']}" + (f", сдать до {till}" if till else ""))
    except Exception as e:
        print(f"{ch.title}: уведомление о заказе не ушло:", str(e)[:200], flush=True)
    return order


def _rate_note(lines, kzt):
    conv = [n for l, (_, n) in zip(lines, kzt) if l["cur"] and l["cur"] not in ("KZT", "398")]
    return ("\nЦена в тенге: " + "; ".join(conv)) if conv else ""


def _economy_safe(ch, lines, mids, kzt):
    try:
        return economy(ch, lines, mids, kzt)
    except Exception as e:
        print(f"{ch.title}: экономика заказа не посчитана:", str(e)[:200], flush=True)
        return f"Экономика заказа: не посчитана ({str(e)[:120]})"


def repair_zero(ch, now=None, days=7):
    """Заказы площадки за days дней, записанные в МойСклад с ценой 0: ставим цену продажи в тенге и экономику в комментарий.
    Меняются только цены позиций и комментарий. → [номера заказов МойСклад]."""
    now, fixed = now or time.time(), []
    with db() as d:                                      # кандидаты — свои записи с итогом 0: площадку и МойСклад спрашиваем только при них
        todo = {r[0] for r in d.run("SELECT posting FROM mp_order WHERE mp=%s AND order_id<>'' AND (total IS NULL OR total=0) "
                                    "AND status<>'cancel' AND created>%s", (ch.key, now - days * 86400), many=True) or []}
    if not todo:
        return fixed
    mp = mapping(ch)
    for o in ch.fetch(now - days * 86400, now + 60):
        rec = get(ch, o["ext"]) if o["ext"] in todo else None
        if not rec or not rec["order_id"] or o["status"] == "cancel":
            continue
        rows = oh.ms("GET", f"/entity/customerorder/{rec['order_id']}/positions", params={"limit": 100}).get("rows") or []
        if not rows or any((r.get("price") or 0) > 0 for r in rows):
            _put(ch, o["ext"], total=round(sum((r.get("price") or 0) * (r.get("quantity") or 1) for r in rows) / 100) or 1)
            continue
        mids, kzt = [ms_id_for(l, mp) for l in o["lines"]], kzt_lines(ch, o["lines"])
        if not all(p for p, _ in kzt) or None in mids:
            print(f"{ch.title}: заказ {rec['number']} — цена всё ещё не получена, оставляю 0", flush=True)
            continue
        price = {mid: p for mid, (p, _) in zip(mids, kzt)}
        total = 0
        for r in rows:
            mid = ((r.get("assortment") or {}).get("meta") or {}).get("href", "").rstrip("/").rsplit("/", 1)[-1]
            if mid in price:
                oh.ms("PUT", f"/entity/customerorder/{rec['order_id']}/positions/{r['id']}", json={"price": round(price[mid] * 100)})
                total += price[mid] * (r.get("quantity") or 1)
        cur = oh.ms("GET", f"/entity/customerorder/{rec['order_id']}")
        desc = ((cur.get("description") or "") + _rate_note(o["lines"], kzt) + "\n\n" + _economy_safe(ch, o["lines"], mids, kzt)
                + "\n(цена исправлена системой AMURA: была 0)").strip()[:4000]
        oh.ms("PUT", f"/entity/customerorder/{rec['order_id']}", json={"description": desc})
        _put(ch, o["ext"], total=round(total))
        fixed.append(rec["number"])
    if fixed:
        print(f"{ch.title}: заказы с ценой 0 исправлены: {', '.join('№' + n for n in fixed)}", flush=True)
    return fixed


# ---------- отгрузка: площадка передала в доставку → отгрузка МойСклад по заказу ----------
def _ship_db():
    d = db()
    d.run("CREATE TABLE IF NOT EXISTS mp_ship (mp TEXT, posting TEXT, demand TEXT DEFAULT '', at DOUBLE PRECISION, PRIMARY KEY (mp, posting))")
    d.c.commit()
    return d


def shipped(ch, ext):
    with _ship_db() as d:
        r = d.run("SELECT demand FROM mp_ship WHERE mp=%s AND posting=%s", (ch.key, ext), one=True)
    return r[0] if r else None


def ship_order(ch, ext, rec):
    """Отгрузка шаблоном МойСклад по заказу (shipments.create): позиции и цены — как в заказе, резерв уходит, остаток списывается.
    Отгрузка уже есть (создал человек или сценарий) — вторую не создаём, только запоминаем."""
    import shipments
    try:
        r = shipments.create(rec["order_id"], who=f"{ch.title}: передан в доставку") or {}
        if r.get("status") == "queued":                  # прошлая попытка не дошла — повторяем ту же операцию, новую не заводим
            r = shipments.send(r["id"]) or {}
        if r.get("status") == "error" and "уже есть отгрузка" in r.get("error", ""):
            num = "уже была"
        elif r.get("status") != "sent":
            raise RuntimeError(r.get("error") or "отгрузка не создана")
        else:
            num = r.get("msNumber") or ""
    except ValueError as e:                              # по заказу уже есть отгрузка (человек или сценарий МойСклад)
        num = "уже была" if "уже" in str(e) else str(e)[:60]
    with _ship_db() as d:
        d.run("INSERT INTO mp_ship (mp, posting, demand, at) VALUES (%s, %s, %s, %s)", (ch.key, ext, num, time.time()))
    print(f"{ch.title}: заказ {rec['number']} ({ext}) передан в доставку — отгрузка {num}", flush=True)
    return num


LABEL_LINE = "Этикетка: прикреплена (Файлы)"


def _label_db():
    d = db()
    d.run("CREATE TABLE IF NOT EXISTS mp_label (mp TEXT, posting TEXT, file TEXT DEFAULT '', at DOUBLE PRECISION, PRIMARY KEY (mp, posting))")
    d.c.commit()
    return d


def labelled(ch, ext):
    with _label_db() as d:
        return bool(d.run("SELECT 1 FROM mp_label WHERE mp=%s AND posting=%s", (ch.key, ext), one=True))


def attach_label(ch, ext, rec):
    """Этикетка площадки → файл заказа МойСклад (открыть и распечатать из приложения МойСклад) + строка в комментарии. Один раз."""
    import base64
    name, data = ch.label(ext)
    oid = rec["order_id"]
    have = oh.ms("GET", f"/entity/customerorder/{oid}/files", params={"limit": 100}).get("rows") or []
    if not any(f.get("filename") == name for f in have):           # прошлая попытка могла дойти — второй файл не нужен
        oh.ms("POST", f"/entity/customerorder/{oid}/files", json=[{"filename": name, "content": base64.b64encode(data).decode()}])
    o = oh.ms("GET", f"/entity/customerorder/{oid}")
    if LABEL_LINE not in (o.get("description") or ""):
        oh.ms("PUT", f"/entity/customerorder/{oid}", json={"description": ((o.get("description") or "") + "\n" + LABEL_LINE).strip()[:4000]})
    with _label_db() as d:
        d.run("INSERT INTO mp_label (mp, posting, file, at) VALUES (%s, %s, %s, %s)", (ch.key, ext, name, time.time()))
    print(f"{ch.title}: этикетка {name} прикреплена к заказу {rec['number']}", flush=True)
    return name


def cancel_order(oid, title):
    """Маркетплейс отменил: заказ в МойСклад «Отменен», резерв по позициям снят."""
    o = oh.ms("GET", f"/entity/customerorder/{oid}")
    body = {"description": ((o.get("description") or "") + f"\nОтменён на {title}").strip()[:4000]}
    st = _state("Отменен")
    if st:
        body["state"] = {"meta": st}
    oh.ms("PUT", f"/entity/customerorder/{oid}", json=body)
    for pos in oh.ms("GET", f"/entity/customerorder/{oid}/positions", params={"limit": 1000}).get("rows") or []:
        if pos.get("reserve"):
            oh.ms("PUT", f"/entity/customerorder/{oid}/positions/{pos['id']}", json={"reserve": 0})


def sync_orders(ch, now=None):
    now = now or time.time()
    start = since_mark(ch)
    mp, made = mapping(ch), 0
    for o in ch.fetch(max(start, now - LOOKBACK_DAYS * 86400) - 3600, now + 60):
        rec = get(ch, o["ext"])
        if rec and rec["order_id"]:
            if o["status"] == "cancel" and rec["status"] != "cancel" and shipped(ch, o["ext"]) is not None:
                _put(ch, o["ext"], status="cancel", error="отменён после отгрузки — возврат")    # отгрузку не трогаем: товар вернётся возвратом
                oh.alert(f"mp-return:{ch.key}:{o['ext']}", f"{ch.title} отменил заказ {rec['number']} ({o['ext']}) уже после отгрузки — "
                                                            f"это возврат: отгрузку не трогаю, товар оформится возвратом", every=86400)
            elif o["status"] == "cancel" and rec["status"] != "cancel":
                try:
                    cancel_order(rec["order_id"], ch.title)
                    _put(ch, o["ext"], status="cancel", error="")
                except Exception as e:
                    _put(ch, o["ext"], error=f"отмена в МойСклад: {str(e)[:250]}")
                    oh.alert(f"mp-cancel:{ch.key}:{o['ext']}", f"{ch.title} отменил заказ {rec['number']} ({o['ext']}), "
                                                               f"а в МойСклад отменить не вышло: {str(e)[:200]}", every=3600)
            elif o["status"] != "cancel" and o["raw"] != rec["status"]:
                _put(ch, o["ext"], status=o["raw"])
            if o["status"] != "cancel" and o.get("label") and not o.get("shipped") and not labelled(ch, o["ext"]):   # нужна до передачи
                try:
                    attach_label(ch, o["ext"], rec)
                except ApiError as e:                    # площадка ещё не выдала этикетку — попробуем при следующей сверке
                    print(f"{ch.title}: этикетка {o['ext']} пока не получена: {e.message[:150]}", flush=True)
                except Exception as e:
                    print(f"{ch.title}: этикетка {o['ext']} не прикреплена: {str(e)[:200]}", flush=True)
            if o["status"] != "cancel" and o.get("shipped") and shipped(ch, o["ext"]) is None:
                try:
                    ship_order(ch, o["ext"], rec)
                except Exception as e:
                    _put(ch, o["ext"], error=f"отгрузка в МойСклад: {str(e)[:250]}")
                    oh.alert(f"mp-ship:{ch.key}:{o['ext']}", f"{ch.title}: заказ {rec['number']} передан в доставку, а отгрузка в МойСклад "
                                                              f"не создалась: {str(e)[:200]}. Повторю через 5 минут", every=3600)
            continue
        if o["at"] and o["at"] < start:
            if not rec:
                _put(ch, o["ext"], status=o["raw"], error="до включения канала — не переносим")
            continue
        if o["status"] in ("cancel", "wait"):
            _put(ch, o["ext"], status="cancel" if o["status"] == "cancel" else o["raw"])
            continue
        if rec and rec["tries"] >= MAX_TRIES:
            continue
        if create_order(ch, o, mp):
            made += 1
    return made


# ---------- остатки: МойСклад → маркетплейс ----------
# ---------- «не выставлять»: товар магазина с остатком 0 ----------
def kiz_on():
    """«Маркировка подключена» (КИЗ). Пока выключено, карточки WB с needKiz получают остаток 0 — собрать заказ без КИЗ нельзя."""
    return setting("mp_kiz_on", "0") == "1"


def ident(ch, x):
    """Товар магазина для отметки: WB — nmID, Ozon — артикул продавца (offer_id)."""
    return str(x.get("nm") or "") if isinstance(ch, WB) else str(x.get("offer") or "")


def noshow(ch):
    try:
        return json.loads(setting(f"mp_{ch.key}_noshow", "") or "{}")
    except ValueError:
        return {}


def set_noshow(ch, idt, hide, who):
    d = noshow(ch)
    if hide:
        d[str(idt)] = {"who": who, "at": int(time.time())}
    else:
        d.pop(str(idt), None)
    set_setting(f"mp_{ch.key}_noshow", json.dumps(d, ensure_ascii=False))
    return d


def hidden(ch, x, marks=None):
    """Почему остаток товара не выставляем (→ 0): отметка «не выставлять» или нужна маркировка. Пусто — выставляем."""
    if ident(ch, x) in (noshow(ch) if marks is None else marks):
        return "не выставлять"
    if isinstance(ch, WB) and x.get("kiz") and not kiz_on():
        return "нужна маркировка"
    return ""


def goods(ch):
    """Товары магазина для экрана «Ozon и WB» (из последней сверки): ident, название, есть ли пара в МойСклад, причина «не выставлять»."""
    m = oh._cache.get(f"mp_map_{ch.key}")
    if not m:
        return None
    mp, marks, out = m[1], noshow(ch), {}
    for ref, x in mp["products"].items():
        i = ident(ch, x)
        if i and not x["archived"] and i not in out:
            out[i] = {"id": i, "offer": x["offer"], "name": x["name"], "pair": bool(mp["ref"].get(ref)), "hidden": hidden(ch, x, marks)}
    return sorted(out.values(), key=lambda g: (not g["hidden"], g["name"].lower()))


def push_stocks(ch, force=False):
    mp, cat, pct, share, marks = mapping(ch), _catalog(), reserve_pct(), share_pct(ch), noshow(ch)
    want = []
    for ref, x in mp["products"].items():
        mid = mp["ref"].get(ref)
        if not mid or x["archived"]:
            continue
        n = 0 if hidden(ch, x, marks) else shown((cat.get(mid) or {}).get("qty"), pct, share)
        if force or ch.sent.get(ref) != n:
            want.append({"ref": ref, "offer": x["offer"], "stock": n})
    errors = ch.push_stocks(want) if want else []
    return {"sent": len(want), "errors": errors[:20]}


# ---------- фон ----------
def _log(ch, kind, ok, info):
    set_setting(f"mp_{ch.key}_{kind}", json.dumps({"at": int(time.time()), "ok": ok, "info": info}, ensure_ascii=False)[:2000])


def tick(now=None):
    now = now or time.time()
    if not _lock.acquire(blocking=False):
        return
    try:
        for ch in channels():
            if not ch.keys_set():
                continue
            if now - ch.last["orders"] >= ORDERS_EVERY:
                ch.last["orders"] = now
                try:
                    _log(ch, "orders", True, {"made": sync_orders(ch, now)})
                except Exception as e:
                    _log(ch, "orders", False, str(e)[:300])
                    oh.alert(f"mp-orders:{ch.key}", f"{ch.title}: заказы не забираются — {str(e)[:250]}. Проверьте ключ на Render", every=3 * 3600)
                if now - ch.last.get("repair", 0) >= 1800:     # заказы с ценой 0 за 7 дней — цена и экономика, как только площадка её отдаст
                    ch.last["repair"] = now
                    try:
                        fixed = repair_zero(ch, now)
                        if fixed:
                            oh.alert(f"mp-fixzero:{ch.key}", f"{ch.title}: в заказах с ценой 0 проставлена цена и экономика: "
                                                             + ", ".join("№" + n for n in fixed), every=0)
                    except Exception as e:
                        print(f"{ch.title}: правка заказов с ценой 0 не удалась:", str(e)[:200], flush=True)
            if now - ch.last["stocks"] >= STOCKS_EVERY:
                ch.last["stocks"] = now
                force = now - ch.sent_at >= 6 * 3600              # раз в 6 часов отправляем все остатки заново
                try:
                    r = push_stocks(ch, force=force)
                    if force:
                        ch.sent_at = now
                    _log(ch, "stocks", not r["errors"], r)
                except Exception as e:
                    _log(ch, "stocks", False, str(e)[:300])
                    oh.alert(f"mp-stocks:{ch.key}", f"{ch.title}: остатки не обновляются — {str(e)[:250]}", every=3 * 3600)
    finally:
        _lock.release()


def _loop():
    while True:
        time.sleep(60)
        try:
            if on():
                tick()
        except Exception as e:
            print("Каналы продаж: фоновая сверка:", str(e)[:200], flush=True)


def start():
    bg.start(_loop)


# ---------- система AMURA: состояние и настройки ----------
def status():
    def last(ch, kind):
        try:
            return json.loads(setting(f"mp_{ch.key}_{kind}", "") or "null")
        except ValueError:
            return None
    out = {"on": modules.enabled("marketplaces"), "reserve": reserve_pct(), "kiz": kiz_on(), "channels": []}
    with db() as d:
        for ch in channels():
            rows = d.run(f"SELECT {COLS} FROM mp_order WHERE mp=%s ORDER BY created DESC LIMIT 20", (ch.key,), many=True) or []
            m = oh._cache.get(f"mp_map_{ch.key}")
            out["channels"].append({"id": ch.key, "title": ch.title, "keys": ch.keys_set(), "env": ch.env_names(),
                                    "share": share_pct(ch), "orders": last(ch, "orders"),
                                    "stocks": last(ch, "stocks"), "lost": (m[1]["lost"][:50] if m else []), "goods": goods(ch),
                                    "recent": [dict(zip(COLS.split(", "), r)) for r in rows]})
    return out


def _me(owner_only=False):
    import admin
    me = admin.who()
    if not me:
        return None, (jsonify(ok=False, error="Войдите заново"), 401)
    if me["role"] != "owner" and (owner_only or "orders" not in me["perms"]):
        return None, (jsonify(ok=False, error="Нет доступа к этому разделу"), 403)
    return me, None


@bp.get("/admin/api/mp/status")
def status_route():
    _, err = _me()
    return err or jsonify(ok=True, **status())


@bp.post("/admin/api/mp/settings")
def settings_route():
    _, err = _me(owner_only=True)
    if err:
        return err
    d = request.get_json(silent=True) or {}
    if "reserve" in d:
        try:
            set_setting("mp_reserve", max(0, min(90, int(d["reserve"]))))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Запас — целое число процентов"), 400
        for ch in channels():
            ch.sent.clear()
    if isinstance(d.get("share"), dict):
        known = {ch.key: ch for ch in channels()}
        for k, v in d["share"].items():
            if k not in known:
                return jsonify(ok=False, error="Нет такого магазина"), 400
            try:
                set_setting(f"mp_{k}_share", max(0, min(100, int(v))))
            except (TypeError, ValueError):
                return jsonify(ok=False, error="Доля остатка — целое число процентов"), 400
            known[k].sent.clear()
    if "kiz" in d:
        set_setting("mp_kiz_on", "1" if d["kiz"] else "0")
    if isinstance(d.get("noshow"), dict):
        x = d["noshow"]
        ch = {c.key: c for c in channels()}.get(str(x.get("shop") or ""))
        if not ch or not str(x.get("id") or "").strip():
            return jsonify(ok=False, error="Нет такого магазина или товара"), 400
        set_noshow(ch, str(x["id"]).strip(), bool(x.get("hide")), "владелец")
    if d.get("run") and on():
        for ch in channels():
            ch.last.update(orders=0, stocks=0)
            oh._cache.pop(f"mp_map_{ch.key}", None)
        oh.notify_bg(tick)
    return jsonify(ok=True, **status())


import mp_calc  # noqa: E402,F401  — калькулятор прибыли: маршруты на том же blueprint
