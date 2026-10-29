"""Модуль «Рабочее место менеджера» (этап 1, 28.10.2026): сессия «AMURA — Менеджер WB/Ozon» работает с маркетплейсами через сервер
системы AMURA — ключи площадок остаются только на Render. Выключен по умолчанию.

API менеджера — /agent/api/*, доступ по заголовку Authorization: Bearer <MP_AGENT_TOKEN> (переменная на Render, отдельная от входа
владельца, действует только на эти маршруты). Проверка — hmac.compare_digest; не больше AGENT_RATE запросов в минуту; каждый вызов —
в журнал (кто, что, когда, ответ), журнал виден владельцу.
- Чтение: GET /agent/api/read/<магазин>/<что> — только методы из списка READ (сверены с документацией WB 28.10.2026 и Ozon Seller API),
  параметры — из разрешённых. /agent/api/calc — калькулятор прибыли. /agent/api/help — описание.
- Действия: менеджер ничего не делает сам — POST /agent/api/requests создаёт заявку. Заявка ждёт «Да» владельца в системе AMURA
  («Заказы → На согласование»), владельцу уходит сообщение в Telegram; после «Да» сервер выполняет действие своим ключом и сохраняет
  ответ площадки. Правила самостоятельности (настройки модуля, по умолчанию выключены): ответы на отзывы 4–5★, ответы на вопросы,
  ставки рекламы в кампаниях с дневным лимитом не выше X ₸. Деньги (пополнение бюджета) — всегда через «Да».
Этапы 2 (КИЗ) и 3 (Kaspi магазин) — не начинать без слова владельца."""
import hmac
import json
import os
import threading
import time

from flask import Blueprint, abort, jsonify, request

import inbox
import marketplaces as mpk
import modules
import order_hook as oh

bp = Blueprint("agent", __name__)
AGENT_RATE = 60                                     # запросов в минуту
_hits, _hits_lock = [], threading.Lock()
_ready = [False]
SET = {"auto_reviews": "agent_auto_reviews", "auto_questions": "agent_auto_questions", "auto_bids": "agent_auto_bids",
       "auto_cards": "agent_auto_cards", "stop": "agent_auto_stop"}
# решение владельца 28.10.2026: отзывы 4–5★, вопросы и ставки в пределах дневного бюджета — без «Да» (включено по умолчанию);
# отзывы 1–3★, бюджет, запуск/остановка кампаний, цены, скидки, акции — всегда через «Да»; «Стоп» выключает всё автоматическое
DEFAULTS = {"auto_reviews": "1", "auto_questions": "1", "auto_bids": "1", "auto_cards": "1", "stop": "0"}   # карточки — слово владельца 29.10.2026


# ---------- база ----------
def db():
    d = inbox.db()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        d.run(f"CREATE TABLE IF NOT EXISTS agent_log (id {pk}, at DOUBLE PRECISION, ip TEXT, method TEXT, path TEXT, args TEXT, status INTEGER)")
        d.run(f"CREATE TABLE IF NOT EXISTS agent_req (id {pk}, at DOUBLE PRECISION, kind TEXT, shop TEXT, params TEXT, summary TEXT,"
              " comment TEXT, status TEXT, result TEXT, decided DOUBLE PRECISION, who TEXT)")
        d.run("CREATE TABLE IF NOT EXISTS agent_corridor (shop TEXT, ident TEXT, pid TEXT DEFAULT '', name TEXT DEFAULT '',"
              " base DOUBLE PRECISION DEFAULT 0, base_note TEXT DEFAULT '', lower DOUBLE PRECISION DEFAULT 0, upper DOUBLE PRECISION DEFAULT 0,"
              " price DOUBLE PRECISION DEFAULT 0, margin DOUBLE PRECISION DEFAULT 0, calc_at DOUBLE PRECISION DEFAULT 0, note TEXT DEFAULT '',"
              " complete INTEGER DEFAULT 0, last_change DOUBLE PRECISION DEFAULT 0, PRIMARY KEY (shop, ident))")
        d.c.commit()
        _ready[0] = True
    return d


def log(status, args=""):
    try:
        with db() as d:
            d.run("INSERT INTO agent_log (at, ip, method, path, args, status) VALUES (%s, %s, %s, %s, %s, %s)",
                  (time.time(), oh.client_ip() if request else "", request.method, request.path, str(args)[:500], status))
    except Exception as e:
        print("Журнал менеджера:", str(e)[:200], flush=True)


def setting(key, default=""):
    with inbox.db() as d:
        return inbox.get_setting(d, key, default)


def budget(key):
    """Дневной бюджет на рекламу магазина, ₸ (задаёт владелец). 0 — не задан: ставки только через «Да»."""
    v = setting(f"agent_bid_budget_{key}", "")
    return int(v) if v.isdigit() else 0


def rules():
    out = {k: setting(v, DEFAULTS[k]) == "1" for k, v in SET.items()}
    out["budgets"] = {c.key: budget(c.key) for c in mpk.channels() if isinstance(c, mpk.WB)}
    return out


# ---------- доступ ----------
def _guard():
    """Модуль выключен — 404; нет токена на Render или чужой токен — 401; чаще AGENT_RATE в минуту — 429."""
    if not modules.enabled("agent"):
        abort(404)
    want = os.environ.get("MP_AGENT_TOKEN", "").strip()
    got = request.headers.get("Authorization", "")
    got = got[7:].strip() if got.lower().startswith("bearer ") else ""
    if not want or not got or not hmac.compare_digest(got.encode(), want.encode()):
        log(401)
        return jsonify(ok=False, error="Нет доступа"), 401
    now = time.time()
    with _hits_lock:
        _hits[:] = [t for t in _hits if now - t < 60]
        if len(_hits) >= AGENT_RATE:
            log(429)
            return jsonify(ok=False, error=f"Не чаще {AGENT_RATE} запросов в минуту"), 429
        _hits.append(now)
    return None


@bp.before_request
def _before():
    if request.path.startswith("/agent/api/"):
        return _guard()


def shop(key, want=None):
    mpk.channels()
    ch = mpk.CHANNELS.get(key or "")
    if not ch or not ch.keys_set() or (want and not isinstance(ch, want)):
        return None
    return ch


# ---------- чтение: только методы из списка ----------
# имя → (площадка, метод, база WB / путь, разрешённые параметры, тело по умолчанию). WB — GET-параметры, Ozon — JSON-тело.
READ = {
    "wb": {
        "orders": ("GET", "statistics", "/api/v1/supplier/orders", ["dateFrom", "flag"], "заказы (Statistics API)"),
        "sales": ("GET", "statistics", "/api/v1/supplier/sales", ["dateFrom", "flag"], "продажи и возвраты (Statistics API)"),
        "fbs_new": ("GET", "marketplace", "/api/v3/orders/new", [], "новые сборочные задания FBS"),
        "warehouses": ("GET", "marketplace", "/api/v3/warehouses", [], "склады продавца"),
        "stocks": ("POST", "marketplace", "/api/v3/stocks/{warehouseId}", ["warehouseId", "chrtIds"], "остатки на складе FBS по chrtId"),
        "cards": ("POST", "content", "/content/v2/get/cards/list", ["settings"], "карточки (Content API)"),
        "charcs": ("GET", "content", "/content/v2/object/charcs/{subjectId}", ["subjectId", "locale"], "характеристики предмета (справочник)"),
        "card_errors": ("POST", "content", "/content/v2/cards/error/list", ["cursor", "order"], "ошибки создания и правки карточек"),
        "cards_trash": ("POST", "content", "/content/v2/get/cards/trash", ["settings"], "карточки в корзине"),
        "prices": ("GET", "discounts-prices", "/api/v2/list/goods/filter", ["limit", "offset", "filterNmID"], "цены и скидки"),
        "feedbacks": ("GET", "feedbacks", "/api/v1/feedbacks", ["isAnswered", "nmId", "take", "skip", "order", "dateFrom", "dateTo"], "отзывы"),
        "questions": ("GET", "feedbacks", "/api/v1/questions", ["isAnswered", "nmId", "take", "skip", "order", "dateFrom", "dateTo"], "вопросы"),
        "unseen": ("GET", "feedbacks", "/api/v1/new-feedbacks-questions", [], "есть ли непросмотренные отзывы и вопросы"),
        "adverts_count": ("GET", "advert", "/adv/v1/promotion/count", [], "списки кампаний"),
        "adverts": ("GET", "advert", "/api/advert/v2/adverts", ["ids", "statuses", "payment_type"], "кампании: товары, ставки"),
        "advert_stats": ("GET", "advert", "/adv/v3/fullstats", ["ids", "beginDate", "endDate"], "статистика кампаний (≤31 дня)"),
        "advert_spend": ("GET", "advert", "/adv/v1/upd", ["from", "to"], "история затрат"),
        "advert_budgets": ("POST", "advert", "/api/advert/v2/budget", ["ids"], "остатки бюджетов кампаний"),
        "daily_limits": ("GET", "advert", "/api/advert/v0/daily-limits", ["advertIds"], "дневные лимиты кампаний"),
        "bid_recommendations": ("GET", "advert", "/api/advert/v0/bids/recommendations", ["nmId", "advertId"], "рекомендуемые ставки"),
        "balance": ("GET", "advert", "/adv/v1/balance", [], "баланс рекламного кабинета"),
        "search_report": ("POST", "seller-analytics", "/api/v2/search-report/report", ["*"], "поисковые запросы: основная страница"),
        "search_texts": ("POST", "seller-analytics", "/api/v2/search-report/product/search-texts", ["*"], "поисковые запросы по товару"),
        "search_orders": ("POST", "seller-analytics", "/api/v2/search-report/product/orders", ["*"], "заказы и позиции по запросам товара"),
        "funnel": ("POST", "seller-analytics", "/api/analytics/v3/sales-funnel/products", ["*"], "воронка карточек за период"),
        "realization": ("POST", "finance", "/api/finance/v1/sales-reports/detailed", ["dateFrom", "dateTo", "limit", "rrdId", "period", "fields"],
                        "детализация отчёта реализации"),
        "promotions": ("GET", "dp-calendar", "/api/v1/calendar/promotions", ["startDateTime", "endDateTime", "allPromo", "limit", "offset"], "акции"),
        "promotion_goods": ("GET", "dp-calendar", "/api/v1/calendar/promotions/nomenclatures", ["promotionID", "inAction", "limit", "offset"],
                            "товары для акции"),
    },
    "ozon": {
        "postings": ("POST", "", "/v4/posting/fbs/list", ["*"], "отправления FBS"),
        "stocks": ("POST", "", "/v4/product/info/stocks", ["*"], "остатки"),
        "products": ("POST", "", "/v3/product/list", ["*"], "список товаров"),
        "product_info": ("POST", "", "/v3/product/info/list", ["*"], "карточки товаров"),
        "prices": ("POST", "", "/v5/product/info/prices", ["*"], "цены, комиссии"),
        "reviews": ("POST", "", "/v1/review/list", ["*"], "отзывы"),
        "review_info": ("POST", "", "/v1/review/info", ["*"], "отзыв"),
        "review_comments": ("POST", "", "/v1/review/comment/list", ["*"], "комментарии к отзыву"),
        "analytics": ("POST", "", "/v1/analytics/data", ["*"], "аналитика (выручка, заказы)"),
        "queries": ("POST", "", "/v1/analytics/product-queries", ["*"], "поисковые запросы по товарам"),
        "transactions": ("POST", "", "/v3/finance/transaction/list", ["*"], "финансовые операции"),
        "actions": ("GET", "", "/v1/actions", [], "акции"),
        "action_candidates": ("POST", "", "/v1/actions/candidates", ["*"], "товары, которые можно добавить в акцию"),
    },
}


def _wb_path(path, args):
    for k in ("warehouseId", "subjectId"):
        if "{" + k + "}" in path:
            path = path.replace("{" + k + "}", str(int(args.pop(k))))
    return path


def read(ch, name, args):
    kind = "wb" if isinstance(ch, mpk.WB) else "ozon"
    spec = READ[kind].get(name)
    if not spec:
        raise KeyError(name)
    method, base, path, allowed, _ = spec
    args = {k: v for k, v in args.items() if "*" in allowed or k in allowed}
    if kind == "wb":
        path = _wb_path(path, args)
        if method == "GET":
            return ch.req("GET", base, path, params=args)
        return ch.req("POST", base, path, json=args.get("_body", args))
    if method == "GET":
        r = __import__("requests").get(ch.api() + path, timeout=30, headers={"Client-Id": ch.env("CLIENT_ID"), "Api-Key": ch.env("API_KEY")})
        j = mpk._json(r)
        if r.status_code >= 400:
            raise mpk.ApiError(r.status_code, str(j.get("message") or f"HTTP {r.status_code}")[:300])
        return j
    return ch.call(path, args)


@bp.get("/agent/api/help")
def help_route():
    log(200)
    return jsonify(ok=True, shops=[{"id": c.key, "title": c.title, "type": "wb" if isinstance(c, mpk.WB) else "ozon", "keys": c.keys_set()}
                                   for c in mpk.channels()],
                   read={k: {n: {"params": v[3], "about": v[4]} for n, v in m.items()} for k, m in READ.items()},
                   actions={k: {"params": v["params"], "about": v["about"]} for k, v in ACTIONS.items()},
                   rules=rules(), rate=f"{AGENT_RATE} запросов в минуту")


@bp.get("/agent/api/read/<key>/<name>")
def read_route(key, name):
    ch = shop(key)
    if not ch:
        log(400, key)
        return jsonify(ok=False, error="Нет такого магазина с ключом"), 400
    try:
        args = json.loads(request.args.get("p") or "{}")
        if not isinstance(args, dict):
            raise ValueError
        # параметры можно передать и обычной строкой запроса (?isAnswered=false&take=50) — p важнее
        args = {**{k: v for k, v in request.args.items() if k != "p"}, **args}
    except ValueError:
        return jsonify(ok=False, error="p — JSON-объект параметров"), 400
    try:
        data = read(ch, name, dict(args))
    except KeyError:
        log(404, name)
        return jsonify(ok=False, error="Такого чтения нет — список в /agent/api/help"), 404
    except mpk.ApiError as e:
        log(502, f"{name}: {e.code}")
        return jsonify(ok=False, error=f"{ch.title}: {e.message}", code=e.code), 502
    log(200, f"{name} {json.dumps(args, ensure_ascii=False)[:300]}")
    return jsonify(ok=True, shop=ch.key, data=data)


@bp.get("/agent/api/calc")
def calc_route():
    import mp_calc
    q = request.args.get("q", "")
    items = mp_calc.search(q)
    if not items:
        log(404, q)
        return jsonify(ok=False, error="Товар не найден в МойСклад"), 404
    pid = items[0]["id"]
    st, prod = mp_calc.settings(), mp_calc.product(pid)
    chans = [mp_calc.channel_calc(ch, pid, prod, st) if ch.keys_set() else {"id": ch.key, "error": "нет ключа"} for ch in mpk.channels()]
    log(200, q)
    return jsonify(ok=True, product=prod, settings=st, channels=chans)


# ---------- действия: только через заявку ----------
def _wb_feedback(ch, p):
    return ch.req("POST", "feedbacks", "/api/v1/feedbacks/answer", json={"id": str(p["id"]), "text": p["text"]})


def _wb_question(ch, p):
    return ch.req("PATCH", "feedbacks", "/api/v1/questions", json={"id": str(p["id"]), "answer": {"text": p["text"]}, "state": "wbRu"})


def _wb_price(ch, p):
    row = {"nmID": int(p["nmID"])}
    for k in ("price", "discount"):
        if p.get(k) is not None:
            row[k] = int(p[k])
    return ch.req("POST", "discounts-prices", "/api/v2/upload/task", json={"data": [row]})


def _wb_get_card(ch, nm):
    cards = ch.req("POST", "content", "/content/v2/get/cards/list",
                   json={"settings": {"cursor": {"limit": 10}, "filter": {"textSearch": str(nm), "withPhoto": -1}}}).get("cards") or []
    c = next((x for x in cards if int(x.get("nmID") or 0) == nm), None)
    if not c:
        raise mpk.ApiError(404, "карточка не найдена в нашем кабинете")
    return c


def _wb_body(c):
    """Тело /content/v2/cards/update из карточки: метод перезаписывает карточку, поэтому передаём всё, что в ней есть;
    характеристики — как {id, value}. Фото, видео и ярлыки этим методом не меняются (документация WB)."""
    body = {k: c[k] for k in ("nmID", "vendorCode", "brand", "title", "description", "dimensions", "sizes", "documents", "gtin", "kizMarked")
            if k in c}
    body["characteristics"] = [{"id": x["id"], "value": x.get("value")} for x in c.get("characteristics") or [] if x.get("id")]
    return body


def _wb_card_text(ch, p):
    """Текст карточки: берём карточку целиком и меняем только title / description."""
    body = _wb_body(_wb_get_card(ch, int(p["nmID"])))
    for k in ("title", "description"):
        if p.get(k):
            body[k] = p[k]
    return ch.req("POST", "content", "/content/v2/cards/update", json=[body])


CARD_FIELDS = ("characteristics", "dimensions", "title", "description")


def _wb_charcs(ch, subject):
    j = ch.req("GET", "content", f"/content/v2/object/charcs/{int(subject)}", params={"locale": "ru"})
    return {int(x["charcID"]): x for x in j.get("data") or [] if x.get("charcID")}


def check_chars(chars, ref):
    """Значения характеристик по справочнику предмета: charcType 1 — список строк (не больше maxCount), 4 — одно число,
    0 — не используется. → список {id, value} или ValueError."""
    out = []
    for x in chars:
        cid = int(x.get("id"))
        r = ref.get(cid)
        if not r:
            raise ValueError(f"характеристики {cid} нет в справочнике предмета")
        t, v = int(r.get("charcType") or 0), x.get("value")
        if t == 0:
            raise ValueError(f"«{r.get('name')}» не используется в этом предмете")
        if t == 4:
            v = v[0] if isinstance(v, list) and len(v) == 1 else v
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ValueError(f"«{r.get('name')}» — одно число" + (f" ({r['unitName']})" if r.get("unitName") else ""))
            v = int(v) if v == int(v) else v
        else:
            v = [str(i).strip() for i in (v if isinstance(v, list) else [v]) if str(i).strip()]
            if not v:
                raise ValueError(f"«{r.get('name')}» — пустое значение")
            if int(r.get("maxCount") or 0) and len(v) > int(r["maxCount"]):
                raise ValueError(f"«{r.get('name')}» — не больше {r['maxCount']} значений")
        out.append({"id": cid, "value": v})
    return out


def _wb_card_update(ch, p):
    """Характеристики (по справочнику предмета), габариты, название, описание. Прежние значения тронутых полей — в ответ
    (поле before) для кнопки «Вернуть как было». p["_restore"] — откат: характеристики заменяются целиком прежним списком."""
    c = _wb_get_card(ch, int(p["nmID"]))
    body, before = _wb_body(c), {}
    if p.get("characteristics") is not None:
        before["characteristics"] = body["characteristics"]
        if p.get("_restore"):
            body["characteristics"] = p["characteristics"]
        else:
            new = check_chars(p["characteristics"], _wb_charcs(ch, c.get("subjectID")))
            ids = {x["id"] for x in new}
            body["characteristics"] = [x for x in body["characteristics"] if x["id"] not in ids] + new
    if p.get("dimensions"):
        before["dimensions"] = body.get("dimensions") or {}
        body["dimensions"] = {**(body.get("dimensions") or {}), **p["dimensions"]}
    for k in ("title", "description"):
        if p.get(k):
            before[k] = body.get(k, "")
            body[k] = p[k]
    res = ch.req("POST", "content", "/content/v2/cards/update", json=[body])
    return {"before": before, "nmID": int(p["nmID"]), "response": res}


def _wb_trash(ch, p):
    """В корзину WB (не удаление: из корзины можно вернуть; WB сам удаляет через 30 дней, если нет остатков). p["_recover"] — вернуть."""
    nms = [int(x) for x in p["nmIDs"]]
    path = "/content/v2/cards/recover" if p.get("_recover") else "/content/v2/cards/delete/trash"
    return {"nmIDs": nms, "response": ch.req("POST", "content", path, json={"nmIDs": nms})}


def _wb_bid(ch, p):
    return ch.req("PATCH", "advert", "/api/advert/v1/bids", json={"bids": [{"advert_id": int(p["advert_id"]), "nm_bids": [
        {"nm_id": int(p["nm_id"]), "bid_kopecks": int(round(float(p["bid"]) * 100)), "placement": p.get("placement", "combined")}]}]})


def _wb_campaign(ch, p):
    act = p["action"]
    return ch.req("GET", "advert", f"/adv/v0/{act}", params={"id": int(p["advert_id"])})


def _wb_deposit(ch, p):
    return ch.req("POST", "advert", "/adv/v1/budget/deposit", params={"id": int(p["advert_id"])},
                  json={"sum": int(p["sum"]), "type": int(p.get("source", 1)), "return": True})


def _wb_promo(ch, p):
    return ch.req("POST", "dp-calendar", "/api/v1/calendar/promotions/upload",
                  json={"data": {"promotionID": int(p["promotionID"]), "uploadNow": True, "nomenclatures": [int(x) for x in p["nmIDs"]]}})


def _oz_review(ch, p):
    return ch.call("/v1/review/comment/create", {"review_id": str(p["id"]), "text": p["text"], "mark_review_as_processed": True})


def _oz_price(ch, p):
    row = {"offer_id": str(p["offer_id"]), "price": str(p["price"])}
    if p.get("old_price") is not None:
        row["old_price"] = str(p["old_price"])
    return ch.call("/v1/product/import/prices", {"prices": [row]})


def _oz_promo(ch, p):
    return ch.call("/v1/actions/products/activate", {"action_id": int(p["action_id"]), "products": [
        {"product_id": int(x["product_id"]), "action_price": float(x["action_price"]), **({"stock": int(x["stock"])} if x.get("stock") else {})}
        for x in p["products"]]})


def _txt(p):
    t = str(p.get("text") or "").strip()
    if not 2 <= len(t) <= 5000:
        raise ValueError("текст ответа — от 2 до 5000 символов")
    return t


# вид → магазин, обязательные параметры, описание, исполнитель, деньги ли (деньги — всегда через «Да»)
ACTIONS = {
    "wb_feedback_answer": {"shop": mpk.WB, "params": ["id", "text"], "about": "ответ на отзыв WB", "run": _wb_feedback},
    "wb_question_answer": {"shop": mpk.WB, "params": ["id", "text"], "about": "ответ на вопрос WB", "run": _wb_question},
    "wb_price": {"shop": mpk.WB, "params": ["nmID", "price?", "discount?"], "about": "цена (₸) и/или скидка (%) WB", "run": _wb_price},
    "wb_card_text": {"shop": mpk.WB, "params": ["nmID", "title?", "description?"], "about": "название и/или описание карточки WB",
                     "run": _wb_card_text},
    "wb_card_trash": {"shop": mpk.WB, "params": ["nmIDs"], "about": "убрать карточки WB в корзину (архив) — всегда через «Да»; "
                      "вернуть — кнопка владельца «Вернуть как было» (/content/v2/cards/recover)", "run": _wb_trash},
    "wb_card_update": {"shop": mpk.WB, "params": ["nmID", "characteristics?", "dimensions?", "title?", "description?"],
                       "about": "карточка WB: характеристики [{id, value}] по справочнику charcs, габариты {length, width, height, weightBrutto}, "
                                "название (≤60), описание. Цену, бренд, vendorCode, штрихкоды и фото не меняет", "run": _wb_card_update},
    "wb_bid": {"shop": mpk.WB, "params": ["advert_id", "nm_id", "bid", "placement?"], "about": "ставка в кампании WB, ₸ (combined/search/recommendations)",
               "run": _wb_bid},
    "wb_campaign": {"shop": mpk.WB, "params": ["advert_id", "action"], "about": "кампания WB: start / pause / stop", "run": _wb_campaign},
    "wb_budget_deposit": {"shop": mpk.WB, "params": ["advert_id", "sum", "source?"], "about": "пополнить бюджет кампании WB, ₸ (всегда через «Да»)",
                          "run": _wb_deposit, "money": True},
    "wb_promotion": {"shop": mpk.WB, "params": ["promotionID", "nmIDs"], "about": "добавить товары в акцию WB", "run": _wb_promo},
    "ozon_review_answer": {"shop": mpk.Ozon, "params": ["id", "text"], "about": "ответ на отзыв Ozon", "run": _oz_review},
    "ozon_price": {"shop": mpk.Ozon, "params": ["offer_id", "price", "old_price?"], "about": "цена Ozon", "run": _oz_price},
    "ozon_promotion": {"shop": mpk.Ozon, "params": ["action_id", "products"], "about": "добавить товары в акцию Ozon (product_id, action_price, stock?)",
                       "run": _oz_promo},
}


def validate(kind, p):
    a = ACTIONS[kind]
    miss = [k for k in a["params"] if not k.endswith("?") and p.get(k) in (None, "", [])]
    if miss:
        raise ValueError("нет параметров: " + ", ".join(miss))
    if kind.endswith("_answer"):
        p["text"] = _txt(p)
    if kind == "wb_campaign" and p["action"] not in ("start", "pause", "stop"):
        raise ValueError("action — start, pause или stop")
    if kind == "wb_price" and p.get("price") is None and p.get("discount") is None:
        raise ValueError("нужна цена или скидка")
    if kind == "wb_card_text" and not (p.get("title") or p.get("description")):
        raise ValueError("нужно название или описание")
    if kind == "wb_card_trash":
        nms = p["nmIDs"] if isinstance(p["nmIDs"], list) else [p["nmIDs"]]
        try:
            p = {"nmIDs": sorted({int(x) for x in nms})}
        except (TypeError, ValueError):
            raise ValueError("nmIDs — список артикулов WB")
        if not 1 <= len(p["nmIDs"]) <= 1000:
            raise ValueError("nmIDs — от 1 до 1000 артикулов")
    if kind == "wb_card_update":
        allowed = {x.rstrip("?") for x in a["params"]}
        bad = [k for k in p if k not in allowed]
        if bad:
            raise ValueError("эти поля через wb_card_update не меняются: " + ", ".join(bad))
        if not any(p.get(k) for k in CARD_FIELDS):
            raise ValueError("нужно хотя бы одно поле: " + ", ".join(CARD_FIELDS))
        if p.get("title") and len(p["title"]) > 60:
            raise ValueError("название — не длиннее 60 символов")
        if p.get("characteristics") is not None and (not isinstance(p["characteristics"], list)
                                                     or not all(isinstance(x, dict) and x.get("id") for x in p["characteristics"])):
            raise ValueError("characteristics — список {id, value}")
        if p.get("dimensions"):
            d = p["dimensions"]
            if not isinstance(d, dict) or set(d) - {"length", "width", "height", "weightBrutto"}:
                raise ValueError("dimensions — {length, width, height, weightBrutto}")
            for k, v in d.items():
                try:
                    ok = float(v) > 0 and (k == "weightBrutto" or float(v) == int(float(v)))
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    raise ValueError(f"{k} — положительное число" + ("" if k == "weightBrutto" else ", целое, см"))
                d[k] = float(v) if k == "weightBrutto" else int(float(v))
    return p


def summary(kind, ch, p):
    a = ACTIONS[kind]["about"]
    detail = {"wb_feedback_answer": lambda: f"отзыв {p['id']}: «{p['text'][:300]}»", "wb_question_answer": lambda: f"вопрос {p['id']}: «{p['text'][:300]}»",
              "ozon_review_answer": lambda: f"отзыв {p['id']}: «{p['text'][:300]}»",
              "wb_price": lambda: f"nmID {p['nmID']}: " + ", ".join(x for x in (f"цена {p['price']} ₸" if p.get("price") is not None else "",
                                                                                 f"скидка {p['discount']} %" if p.get("discount") is not None else "") if x),
              "ozon_price": lambda: f"{p['offer_id']}: цена {p['price']}" + (f", до скидки {p['old_price']}" if p.get("old_price") else ""),
              "wb_bid": lambda: f"кампания {p['advert_id']}, nmID {p['nm_id']}: ставка {p['bid']} ₸ ({p.get('placement', 'combined')})",
              "wb_campaign": lambda: f"кампания {p['advert_id']}: {dict(start='запустить', pause='пауза', stop='завершить')[p['action']]}",
              "wb_budget_deposit": lambda: f"кампания {p['advert_id']}: пополнить на {p['sum']} ₸",
              "wb_card_text": lambda: f"nmID {p['nmID']}: " + ("название «" + p["title"][:120] + "» " if p.get("title") else "") + ("новое описание" if p.get("description") else ""),
              "wb_card_trash": lambda: ("вернуть из корзины" if p.get("_recover") else "в корзину") + " nmID " + ", ".join(map(str, p["nmIDs"]))[:300],
              "wb_card_update": lambda: f"nmID {p['nmID']}: " + "; ".join(x for x in (
                  ("характеристики " + ", ".join(f"{c['id']}={c.get('value')}" for c in p["characteristics"])[:300]) if p.get("characteristics") else "",
                  ("габариты " + ", ".join(f"{k} {v}" for k, v in p["dimensions"].items())) if p.get("dimensions") else "",
                  ("название «" + p["title"] + "»") if p.get("title") else "", "новое описание" if p.get("description") else "") if x),
              "wb_promotion": lambda: f"акция {p['promotionID']}: товары {', '.join(map(str, p['nmIDs']))[:200]}",
              "ozon_promotion": lambda: f"акция {p['action_id']}: {len(p['products'])} товаров"}[kind]()
    return f"{ch.title}: {a} — {detail}"


def auto_ok(kind, ch, p):
    """Можно ли без «Да» по правилам владельца. Деньги — никогда. Оценку отзыва и лимит кампании проверяем у площадки сами."""
    if ACTIONS[kind].get("money"):
        return False
    r = rules()
    if r["stop"]:
        return False
    if kind == "wb_card_update":
        return r["auto_cards"]
    if kind == "wb_question_answer":
        return r["auto_questions"]
    if kind == "wb_feedback_answer" and r["auto_reviews"]:
        fb = (ch.req("GET", "feedbacks", "/api/v1/feedback", params={"id": str(p["id"])}).get("data") or {})
        return int(fb.get("productValuation") or 0) >= 4
    if kind == "ozon_review_answer" and r["auto_reviews"]:
        return int(ch.call("/v1/review/info", {"review_id": str(p["id"])}).get("rating") or 0) >= 4
    if kind == "wb_bid" and r["auto_bids"] and budget(ch.key) > 0:
        lim = ch.req("GET", "advert", "/api/advert/v0/daily-limits", params={"advertIds": str(int(p["advert_id"]))})
        x = next((a for a in lim.get("adverts") or [] if int(a.get("advertId") or 0) == int(p["advert_id"])), None)
        return bool(x and x.get("enabled") and 0 < float(x.get("dailyLimit") or 0) <= budget(ch.key))   # без лимита — только через «Да»
    return False


def execute(rid, who):
    """Выполнить одобренную заявку ключом магазина; результат и статус — в заявку."""
    with db() as d:
        row = d.run("SELECT kind, shop, params FROM agent_req WHERE id=%s", (rid,), one=True)
    kind, key, params = row[0], row[1], json.loads(row[2])
    ch = shop(key, ACTIONS[kind]["shop"])
    try:
        if not ch:
            raise mpk.ApiError(0, "у магазина нет ключа на Render")
        res, status = ACTIONS[kind]["run"](ch, params), "done"
    except (mpk.ApiError, ValueError, KeyError, TypeError) as e:
        res, status = {"error": getattr(e, "message", str(e))}, "error"
    with db() as d:
        txt = json.dumps(res, ensure_ascii=False, default=str)
        d.run("UPDATE agent_req SET status=%s, result=%s, decided=%s, who=%s WHERE id=%s",   # карточка — целиком: в ней прежняя версия для отката
              (status, txt if kind == "wb_card_update" else txt[:4000], time.time(), who, rid))
    if status == "done" and kind in PRICE_KINDS:
        price_changed(key, ident_of(kind, params))
    return status, res


@bp.post("/agent/api/requests")
def create_route():
    b = request.get_json(silent=True) or {}
    kind, key, p = b.get("kind"), b.get("shop"), dict(b.get("params") or {})
    if kind not in ACTIONS:
        log(400, kind)
        return jsonify(ok=False, error="Такого действия нет — список в /agent/api/help"), 400
    ch = shop(key, ACTIONS[kind]["shop"])
    if not ch:
        log(400, f"{kind} {key}")
        return jsonify(ok=False, error="Нет такого магазина с ключом для этого действия"), 400
    try:
        p = validate(kind, p)
        text = summary(kind, ch, p)
    except (ValueError, KeyError, TypeError) as e:
        log(400, f"{kind}: {e}")
        return jsonify(ok=False, error=str(e)), 400
    rid, status, auto, res = new_request(kind, ch, p, str(b.get("comment") or ""), text)
    log(200, f"{kind} #{rid}" + (f" auto {status}" if auto else ""))
    return jsonify(ok=True, id=rid, status=status, auto=auto, **({"result": res} if auto else {}))


def new_request(kind, ch, p, comment, text):
    """Заявка: по правилу — выполняем сразу; иначе — в «На согласование» и сообщение владельцу. → (№, статус, авто, ответ)."""
    why = ""
    try:
        if kind in PRICE_KINDS:
            auto, why = price_auto(kind, ch, p)
        else:
            auto = auto_ok(kind, ch, p)
    except (mpk.ApiError, ValueError, KeyError) as e:
        auto, why = False, (f"коридор не проверен: {getattr(e, 'message', str(e))}" if kind in PRICE_KINDS else "")
    if why:
        text += f" ({why})"
    with db() as d:
        rid = d.run("INSERT INTO agent_req (at, kind, shop, params, summary, comment, status, result, decided, who) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'new', '', 0, '')",
                    (time.time(), kind, ch.key, json.dumps(p, ensure_ascii=False), text, comment[:1000]), ins=True)
    if auto:
        status, res = execute(rid, "правило владельца")
        return rid, status, True, res
    oh.notify_bg(lambda: oh.tg("sendMessage", chat_id=oh.OWNER,
                               text=f"🧑‍💼 Менеджер WB/Ozon просит согласовать (№ {rid}):\n{text}"
                                    + (f"\nКомментарий: {comment}" if comment else "")
                                    + "\n\nСистема AMURA → Заказы → На согласование"))
    return rid, "new", False, None


COLS = "id, at, kind, shop, params, summary, comment, status, result, decided, who"


def rows(status=None, limit=100):
    with db() as d:
        q = f"SELECT {COLS} FROM agent_req" + (" WHERE status=%s" if status else "") + " ORDER BY id DESC LIMIT %s"
        out = d.run(q, ((status, limit) if status else (limit,)), many=True) or []
    res = []
    for r in out:
        x = dict(zip(COLS.split(", "), r))
        x["params"] = json.loads(x["params"] or "{}")
        try:
            x["result"] = json.loads(x["result"]) if x["result"] else None
        except ValueError:
            pass
        res.append(x)
    return res


@bp.get("/agent/api/requests")
def list_route():
    log(200)
    return jsonify(ok=True, items=rows(request.args.get("status") or None))


# ---------- цены в коридоре (правило владельца 28.10.2026, по умолчанию выключено) ----------
PRICE_KINDS = ("wb_price", "ozon_price")
PR = {"on": ("agent_price_rule", "0"), "margin": ("agent_price_margin", "20"), "margin_new": ("agent_price_margin_new", "15")}
STEP_PCT, GAP, UP_PCT, NEW_DAYS = 5, 86400, 15, 30
CCOLS = "shop, ident, pid, name, base, base_note, lower, upper, price, margin, calc_at, note, complete, last_change"


def price_rules():
    v = {k: setting(key, dflt) for k, (key, dflt) in PR.items()}
    num = lambda x, d: float(x) if x.replace(".", "", 1).isdigit() else d
    return {"on": v["on"] == "1", "margin": num(v["margin"], 20.0), "margin_new": num(v["margin_new"], 15.0)}


def ident_of(kind, p):
    return str(int(p["nmID"])) if kind == "wb_price" else str(p["offer_id"])


def corr_row(key, ident):
    with db() as d:
        r = d.run(f"SELECT {CCOLS} FROM agent_corridor WHERE shop=%s AND ident=%s", (key, ident), one=True)
    return dict(zip(CCOLS.split(", "), r)) if r else None


def corr_put(key, ident, **f):
    with db() as d:
        if d.run("SELECT 1 FROM agent_corridor WHERE shop=%s AND ident=%s", (key, ident), one=True):
            d.run(f"UPDATE agent_corridor SET {', '.join(k + '=%s' for k in f)} WHERE shop=%s AND ident=%s", (*f.values(), key, ident))
        else:
            d.run(f"INSERT INTO agent_corridor (shop, ident{''.join(', ' + k for k in f)}) VALUES (%s, %s{', %s' * len(f)})", (key, ident, *f.values()))


def price_changed(key, ident):
    corr_put(key, ident, last_change=time.time())


def ms_match(ch, ident):
    """Товар площадки → (id товара МойСклад, ключи для первой продажи: nmID / SKU Ozon)."""
    mp = mpk.mapping(ch)
    if isinstance(ch, mpk.WB):
        ref = next((r for r, x in mp["products"].items() if str(x.get("nm")) == ident), None)
        return (mp["ref"].get(ref) if ref else None), [ident]
    ref = next((r for r, x in mp["products"].items() if x.get("offer") == ident), None)
    return mp["offer"].get(ident) or (mp["ref"].get(ref) if ref else None), ((mp["products"].get(ref) or {}).get("skus") or []) if ref else []


def cur_price(ch, ident):
    """Текущая цена для покупателя в кабинете → (цена, скидка продавца %, цена до скидки)."""
    if isinstance(ch, mpk.WB):
        g = ((ch.req("GET", "discounts-prices", "/api/v2/list/goods/filter", params={"limit": 1, "filterNmID": int(ident)})
              .get("data") or {}).get("listGoods") or [{}])[0]
        sz = (g.get("sizes") or [{}])[0]
        return float(sz.get("discountedPrice") or sz.get("price") or 0), float(g.get("discount") or 0), float(sz.get("price") or 0)
    r = ch.call("/v5/product/info/prices", {"filter": {"offer_id": [ident], "visibility": "ALL"}, "cursor": "", "limit": 1})
    it = ((r.get("items") or ch.res(r).get("items") or [{}])[0]).get("price") or {}
    v = float(it.get("price") or 0)
    return v, 0.0, v


def corridor(ch, ident):
    """Пересчёт коридора товара: нижняя — минимальная цена калькулятора для маржи (20 %, товар моложе 30 дней с первой продажи —
    15 %; настройки владельца), верхняя — базовая +15 %. Базовую задаёт владелец, иначе — цена на момент первого расчёта."""
    import mp_calc
    from datetime import datetime, timedelta
    pid, keys = ms_match(ch, ident)
    if not pid:
        raise ValueError("товар площадки не связан с товаром МойСклад")
    f = mp_calc.facts(ch)                              # первая продажа — из отчёта реализации WB / транзакций Ozon за 90 дней
    firsts = [x for x in ((f.get("first") or {}).get(str(k)) for k in keys) if x]
    young = bool(firsts) and min(firsts) >= (datetime.now(mpk.ALMATY) - timedelta(days=NEW_DAYS)).strftime("%Y-%m-%d")
    pr = price_rules()                                 # продаж ещё нет или отчёт не загружен — строже: обычная маржа
    margin = pr["margin_new"] if young else pr["margin"]
    prod = mp_calc.product(pid)
    c = mp_calc.channel_calc(ch, pid, prod, dict(mp_calc.settings(), margin=margin))
    price, disc, _ = cur_price(ch, ident)
    old = corr_row(ch.key, ident) or {}
    base, base_note = old.get("base") or 0, old.get("base_note") or ""
    if not base:
        base, base_note = price, f"цена на момент включения правила ({datetime.now(mpk.ALMATY):%d.%m.%Y})"
    lower = c.get("minPrice") if not c.get("error") else None
    missing = [x["label"] for x in c.get("lines") or [] if x.get("sum") is None]
    note = (c.get("error") or "") or (f"маржа {margin:g} %" + (" (товар моложе 30 дней)" if young else "")
                                      + ("; без строк: " + ", ".join(missing) if missing else ""))
    row = {"pid": pid, "name": prod["name"], "base": base, "base_note": base_note, "lower": lower or 0, "upper": round(base * (1 + UP_PCT / 100)),
           "price": price, "margin": margin, "calc_at": time.time(), "note": note, "complete": 1 if (lower and not missing) else 0}
    corr_put(ch.key, ident, **row)
    return dict(row, disc=disc, shop=ch.key, ident=ident, last_change=old.get("last_change") or 0)


def price_auto(kind, ch, p):
    """Цена без «Да», если правило включено, цена внутри коридора, шаг не больше 5 % и цену не меняли сутки. → (можно ли, почему)."""
    if not price_rules()["on"]:
        return False, ""
    if rules()["stop"]:
        return False, "автоматическое остановлено"
    ident = ident_of(kind, p)
    c = corridor(ch, ident)
    if kind == "wb_price":
        if p.get("price") is None or (p.get("discount") is not None and float(p["discount"]) != c["disc"]):
            return False, "меняется скидка — через «Да»"
        new = float(p["price"]) * (1 - c["disc"] / 100)
    else:
        new = float(p["price"])
    if not c["complete"]:
        return False, f"нижняя граница неполная ({c['note']}) — через «Да»"
    if new < c["lower"]:
        return False, f"ниже коридора: минимум {round(c['lower'])} ₸"
    if new > c["upper"]:
        return False, f"выше коридора: максимум {round(c['upper'])} ₸"
    step = abs(new - c["price"]) / c["price"] * 100 if c["price"] else 100
    if step > STEP_PCT:
        return False, f"шаг {step:.1f} % — больше {STEP_PCT} %"
    if time.time() - (c["last_change"] or 0) < GAP:
        return False, "цену этого товара меняли меньше суток назад"
    return True, f"в коридоре {round(c['lower'])}–{round(c['upper'])} ₸, шаг {step:.1f} %"


def recheck_corridors():
    """Раз в день: нижняя граница выросла выше текущей цены — заявка на повышение владельцу на «Да» (сами не поднимаем)."""
    if not price_rules()["on"]:
        return 0
    with db() as d:
        items = d.run("SELECT shop, ident FROM agent_corridor", many=True) or []
    made = 0
    for key, ident in items:
        ch = shop(key)
        if not ch:
            continue
        try:
            c = corridor(ch, ident)
        except (mpk.ApiError, ValueError) as e:
            print(f"Коридор {key} {ident}:", str(e)[:200], flush=True)
            continue
        if not c["lower"] or c["lower"] <= c["price"]:
            continue
        kind = "wb_price" if isinstance(ch, mpk.WB) else "ozon_price"
        with db() as d:
            open_ = [r for r in d.run("SELECT params FROM agent_req WHERE status='new' AND kind=%s AND shop=%s", (kind, key), many=True) or []
                     if ident_of(kind, json.loads(r[0])) == ident]
        if open_:
            continue
        import math
        p = ({"nmID": int(ident), "price": int(math.ceil(c["lower"] / (1 - c["disc"] / 100)))} if kind == "wb_price"
             else {"offer_id": ident, "price": int(math.ceil(c["lower"]))})
        text = summary(kind, ch, p) + f" — нижняя граница коридора {round(c['lower'])} ₸ выше текущей цены {round(c['price'])} ₸"
        with db() as d:
            rid = d.run("INSERT INTO agent_req (at, kind, shop, params, summary, comment, status, result, decided, who) "
                        "VALUES (%s, %s, %s, %s, %s, %s, 'new', '', 0, '')",
                        (time.time(), kind, key, json.dumps(p, ensure_ascii=False), text, "пересчёт коридора: ставки и себестоимость изменились"),
                        ins=True)
        oh.notify_bg(lambda rid=rid, text=text: oh.tg("sendMessage", chat_id=oh.OWNER,
                                                      text=f"📈 Нужно поднять цену (№ {rid}):\n{text}\n\nСистема AMURA → Заказы → На согласование"))
        made += 1
    return made


# ---------- система AMURA: владелец ----------
def _owner():
    import admin
    me = admin.who()
    if not me:
        return jsonify(ok=False, error="Войдите заново"), 401
    if me["role"] != "owner":
        return jsonify(ok=False, error="Только владелец"), 403
    if not modules.enabled("agent"):
        return jsonify(ok=False, error="Модуль выключен"), 404
    return None


@bp.get("/admin/api/agent")
def owner_view():
    err = _owner()
    if err:
        return err
    with db() as d:
        lg = d.run("SELECT at, ip, method, path, args, status FROM agent_log ORDER BY id DESC LIMIT 100", many=True) or []
    return jsonify(ok=True, items=rows(request.args.get("status") or None), rules=rules(),
                   wbShops=[{"key": c.key, "title": c.title} for c in mpk.channels() if isinstance(c, mpk.WB)],
                   shops=[{"key": c.key, "title": c.title, "wb": isinstance(c, mpk.WB)} for c in mpk.channels() if c.keys_set()],
                   priceRules=price_rules(), corridor=corr_rows(),
                   token=bool(os.environ.get("MP_AGENT_TOKEN", "").strip()),
                   log=[dict(zip(("at", "ip", "method", "path", "args", "status"), r)) for r in lg])


@bp.post("/admin/api/agent/<int:rid>")
def decide(rid):
    err = _owner()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    with db() as d:
        row = d.run("SELECT status, kind, params FROM agent_req WHERE id=%s", (rid,), one=True)
    if not row:
        return jsonify(ok=False, error="Нет такой заявки"), 404
    if row[0] != "new":
        return jsonify(ok=False, error="Заявка уже решена"), 409
    if b.get("decision") == "no":
        with db() as d:
            d.run("UPDATE agent_req SET status='rejected', decided=%s, who='владелец' WHERE id=%s", (time.time(), rid))
        return jsonify(ok=True, status="rejected")
    if b.get("decision") != "yes":
        return jsonify(ok=False, error="decision — yes или no"), 400
    if b.get("text") and row[1].endswith("_answer"):                  # владелец поправил текст ответа
        p = json.loads(row[2])
        p["text"] = b["text"]
        try:
            p = validate(row[1], p)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        with db() as d:
            d.run("UPDATE agent_req SET params=%s WHERE id=%s", (json.dumps(p, ensure_ascii=False), rid))
    status, res = execute(rid, "владелец")
    return jsonify(ok=True, status=status, result=res)


@bp.post("/admin/api/agent/rules")
def set_rules():
    err = _owner()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    wb = {c.key for c in mpk.channels() if isinstance(c, mpk.WB)}
    with inbox.db() as d:
        for k in ("auto_reviews", "auto_questions", "auto_bids", "stop"):
            if k in b:
                inbox.set_setting(d, SET[k], "1" if b[k] else "0")
        for key, v in (b.get("budgets") or {}).items():
            if key not in wb:
                return jsonify(ok=False, error="Нет такого магазина WB"), 400
            try:
                v = max(0, int(v or 0))
            except (TypeError, ValueError):
                return jsonify(ok=False, error="Бюджет — целое число ₸"), 400
            inbox.set_setting(d, f"agent_bid_budget_{key}", str(v))
    return jsonify(ok=True, rules=rules())


# ---------- сводка владельцу раз в день: что сделано без «Да» ----------
DIGEST_HOUR = 21                                     # по Алматы


def digest(now=None):
    """Сообщение владельцу: заявки, выполненные по правилам за последние сутки. Пусто — ничего не шлём."""
    now = now or time.time()
    with db() as d:
        got = d.run("SELECT id, summary, status FROM agent_req WHERE who='правило владельца' AND decided>=%s ORDER BY id",
                    (now - 86400,), many=True) or []
    if not got:
        return ""
    lines = [f"№ {i} {'✓' if st == 'done' else '✗'} {sm[:300]}" for i, sm, st in got]
    return (f"🧑‍💼 Менеджер WB/Ozon без согласования за сутки ({len(got)}):\n" + "\n".join(lines[:40])
            + ("\n…" if len(lines) > 40 else "") + "\n\nОстановить всё автоматическое: Заказы → На согласование → «Стоп»")


def _digest_loop():
    while True:
        time.sleep(600)
        try:
            from datetime import datetime
            t = datetime.now(mpk.ALMATY)
            day = t.strftime("%Y-%m-%d")
            if t.hour >= 9 and modules.enabled("agent") and setting("agent_recheck_day", "") != day:
                with inbox.db() as d:
                    inbox.set_setting(d, "agent_recheck_day", day)
                recheck_corridors()
            if t.hour >= DIGEST_HOUR and modules.enabled("agent") and setting("agent_digest_day", "") != day:
                text = digest()
                with inbox.db() as d:
                    inbox.set_setting(d, "agent_digest_day", day)
                if text:
                    oh.tg("sendMessage", chat_id=oh.OWNER, text=text)
        except Exception as e:
            print("Сводка менеджера:", str(e)[:200], flush=True)


if os.environ.get("PORT"):
    import bg
    bg.start(_digest_loop)


def corr_rows():
    with db() as d:
        rs = d.run(f"SELECT {CCOLS} FROM agent_corridor ORDER BY shop, name", many=True) or []
    return [dict(zip(CCOLS.split(", "), r)) for r in rs]


@bp.post("/admin/api/agent/price-rules")
def set_price_rules():
    err = _owner()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    with inbox.db() as d:
        if "on" in b:
            inbox.set_setting(d, PR["on"][0], "1" if b["on"] else "0")
        for k in ("margin", "margin_new"):
            if k in b:
                try:
                    v = float(b[k])
                except (TypeError, ValueError):
                    return jsonify(ok=False, error="Маржа — числом, %"), 400
                if not 0 <= v < 90:
                    return jsonify(ok=False, error="Маржа — от 0 до 89 %"), 400
                inbox.set_setting(d, PR[k][0], f"{v:g}")
    return jsonify(ok=True, priceRules=price_rules())


@bp.post("/admin/api/agent/corridor")
def set_corridor():
    """Добавить товар в коридор / задать базовую цену / пересчитать: {shop, ident (nmID WB или offer_id Ozon), base?}."""
    err = _owner()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    ch, ident = shop(str(b.get("shop") or "")), str(b.get("ident") or "").strip()
    if not ch or not ident:
        return jsonify(ok=False, error="Укажите магазин и артикул (WB — nmID, Ozon — артикул продавца)"), 400
    if b.get("base") not in (None, ""):
        try:
            base = max(0.0, float(b["base"]))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Базовая цена — числом, ₸"), 400
        corr_put(ch.key, ident, base=base, base_note="задана владельцем" if base else "")
    try:
        row = corridor(ch, ident)
    except (mpk.ApiError, ValueError) as e:
        return jsonify(ok=False, error=getattr(e, "message", str(e))), 400
    return jsonify(ok=True, row=row)


@bp.post("/admin/api/agent/<int:rid>/revert")
def revert(rid):
    """«Вернуть как было» для изменения карточки WB: прежние значения тронутых полей (сохранены в ответе заявки) — обратно."""
    err = _owner()
    if err:
        return err
    with db() as d:
        row = d.run("SELECT kind, shop, status, result FROM agent_req WHERE id=%s", (rid,), one=True)
    if not row or row[0] not in ("wb_card_update", "wb_card_trash") or row[2] != "done":
        return jsonify(ok=False, error="Вернуть можно только выполненное изменение карточки или перенос в корзину"), 400
    res = json.loads(row[3] or "{}")
    if res.get("reverted"):
        return jsonify(ok=False, error=f"Уже возвращено (заявка № {res['reverted']})"), 409
    ch = shop(row[1], mpk.WB)
    if row[0] == "wb_card_trash":
        p = {"nmIDs": res.get("nmIDs") or [], "_recover": True}
        what = f"вернуть из корзины — nmID {', '.join(map(str, p['nmIDs']))[:200]}"
        enough = bool(p["nmIDs"])
    else:
        before = res.get("before") or {}
        p = {"nmID": res.get("nmID"), "_restore": True, **{k: v for k, v in before.items() if v not in (None, {})}}
        what, enough = f"вернуть карточку как было — nmID {p['nmID']}", len(p) > 2
    if not ch or not enough:
        return jsonify(ok=False, error="Нечего возвращать или у магазина нет ключа"), 400
    with db() as d:
        nid = d.run("INSERT INTO agent_req (at, kind, shop, params, summary, comment, status, result, decided, who) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'new', '', 0, '')",
                    (time.time(), row[0], ch.key, json.dumps(p, ensure_ascii=False), f"{ch.title}: {what}", f"откат заявки № {rid}"), ins=True)
    status, out = execute(nid, "владелец (откат)")
    if status == "done":
        res["reverted"] = nid
        with db() as d:
            d.run("UPDATE agent_req SET result=%s WHERE id=%s", (json.dumps(res, ensure_ascii=False, default=str), rid))
    return jsonify(ok=status == "done", id=nid, status=status, result=out, **({} if status == "done" else {"error": "WB не принял откат"}))
