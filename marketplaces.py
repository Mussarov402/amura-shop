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
                                                            "cursor": cursor, "sort_dir": "ASC"}))
            for p in r.get("postings") or []:
                if p.get("posting_number"):
                    out.append(self.norm(p))
            cursor = r.get("cursor") or ""
            if not r.get("has_next") or not cursor:
                break
        return out

    def norm(self, p):
        raw = str(p.get("status_alias") or p.get("status") or "")
        lines = []
        for x in p.get("products") or []:
            try:
                price = float(x.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            lines.append({"ref": str(x.get("product_id") or ""), "offer": str(x.get("product_offer_id") or x.get("offer_id") or ""),
                          "barcodes": [], "name": str(x.get("product_name") or x.get("name") or ""), "qty": int(x.get("quantity") or 0),
                          "price": price, "cur": str(x.get("marketplace_seller_price_currency_code") or x.get("currency_code") or "")})
        return {"ext": str(p["posting_number"]), "label": f"Отправление {p['posting_number']}" + (f", заказ Ozon {p['order_number']}" if p.get("order_number") else ""),
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
            price, cur = (o.get("price") or 0) / 100, "KZT" if o.get("currencyCode") == self.KZT else str(o.get("currencyCode") or "")
        else:
            price = (o.get("convertedPrice") or 0) / 100
            cur = "KZT" if o.get("convertedCurrencyCode") == self.KZT else str(o.get("convertedCurrencyCode") or "")
        return {"ext": str(o["id"]), "label": f"Сборочное задание {o['id']}" + (f", заказ WB {o['orderUid']}" if o.get("orderUid") else ""),
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
                                                 "nm": c.get("nmID"), "subject": c.get("subjectID"), "dims": c.get("dimensions") or {}}
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
                lost.append({"offer": x["offer"], "name": x["name"]})
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
    for l in lines:
        mid = ms_id_for(l, mp)
        free = int((cat.get(mid) or {}).get("qty") or 0)
        positions.append({"quantity": l["qty"], "price": round(l["price"] * 100), "reserve": min(l["qty"], max(free, 0)),
                          "assortment": oh.meta("product", mid)})
        total += l["price"] * l["qty"]
    cur = {l["cur"] for l in lines if l["cur"]}
    till = datetime.fromtimestamp(o["cutoff"], ALMATY).strftime("%d.%m %H:%M") if o["cutoff"] else ""
    desc = (f"{ch.mark}\n{o['label']}" + (f"\nСобрать и сдать до: {till}" if till else "")
            + (f"\nЦены {ch.title} в {', '.join(sorted(cur))}" if cur and cur != {"KZT"} else ""))
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
            if o["status"] == "cancel" and rec["status"] != "cancel":
                try:
                    cancel_order(rec["order_id"], ch.title)
                    _put(ch, o["ext"], status="cancel", error="")
                except Exception as e:
                    _put(ch, o["ext"], error=f"отмена в МойСклад: {str(e)[:250]}")
                    oh.alert(f"mp-cancel:{ch.key}:{o['ext']}", f"{ch.title} отменил заказ {rec['number']} ({o['ext']}), "
                                                               f"а в МойСклад отменить не вышло: {str(e)[:200]}", every=3600)
            elif o["status"] != "cancel" and o["raw"] != rec["status"]:
                _put(ch, o["ext"], status=o["raw"])
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
def push_stocks(ch, force=False):
    mp, cat, pct, share = mapping(ch), _catalog(), reserve_pct(), share_pct(ch)
    want = []
    for ref, x in mp["products"].items():
        mid = mp["ref"].get(ref)
        if not mid or x["archived"]:
            continue
        n = shown((cat.get(mid) or {}).get("qty"), pct, share)
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
    out = {"on": modules.enabled("marketplaces"), "reserve": reserve_pct(), "channels": []}
    with db() as d:
        for ch in channels():
            rows = d.run(f"SELECT {COLS} FROM mp_order WHERE mp=%s ORDER BY created DESC LIMIT 20", (ch.key,), many=True) or []
            m = oh._cache.get(f"mp_map_{ch.key}")
            out["channels"].append({"id": ch.key, "title": ch.title, "keys": ch.keys_set(), "env": ch.env_names(),
                                    "share": share_pct(ch), "orders": last(ch, "orders"),
                                    "stocks": last(ch, "stocks"), "lost": (m[1]["lost"][:50] if m else []),
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
    if d.get("run") and on():
        for ch in channels():
            ch.last.update(orders=0, stocks=0)
            oh._cache.pop(f"mp_map_{ch.key}", None)
        oh.notify_bg(tick)
    return jsonify(ok=True, **status())


import mp_calc  # noqa: E402,F401  — калькулятор прибыли: маршруты на том же blueprint
