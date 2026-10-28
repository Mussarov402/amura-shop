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
SET = {"auto_reviews": "agent_auto_reviews", "auto_questions": "agent_auto_questions", "auto_bids": "agent_auto_bids", "stop": "agent_auto_stop"}
# решение владельца 28.10.2026: отзывы 4–5★, вопросы и ставки в пределах дневного бюджета — без «Да» (включено по умолчанию);
# отзывы 1–3★, бюджет, запуск/остановка кампаний, цены, скидки, акции — всегда через «Да»; «Стоп» выключает всё автоматическое
DEFAULTS = {"auto_reviews": "1", "auto_questions": "1", "auto_bids": "1", "stop": "0"}


# ---------- база ----------
def db():
    d = inbox.db()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        d.run(f"CREATE TABLE IF NOT EXISTS agent_log (id {pk}, at DOUBLE PRECISION, ip TEXT, method TEXT, path TEXT, args TEXT, status INTEGER)")
        d.run(f"CREATE TABLE IF NOT EXISTS agent_req (id {pk}, at DOUBLE PRECISION, kind TEXT, shop TEXT, params TEXT, summary TEXT,"
              " comment TEXT, status TEXT, result TEXT, decided DOUBLE PRECISION, who TEXT)")
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
    for k in ("warehouseId",):
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


def _wb_card_text(ch, p):
    """Текст карточки: берём карточку целиком (метод перезаписывает её) и меняем только title / description."""
    nm = int(p["nmID"])
    cards = ch.req("POST", "content", "/content/v2/get/cards/list",
                   json={"settings": {"cursor": {"limit": 10}, "filter": {"textSearch": str(nm), "withPhoto": -1}}}).get("cards") or []
    c = next((x for x in cards if int(x.get("nmID") or 0) == nm), None)
    if not c:
        raise mpk.ApiError(404, "карточка не найдена в нашем кабинете")
    body = {k: c[k] for k in ("nmID", "vendorCode", "brand", "title", "description", "dimensions", "characteristics", "sizes") if k in c}
    for k in ("title", "description"):
        if p.get(k):
            body[k] = p[k]
    return ch.req("POST", "content", "/content/v2/cards/update", json=[body])


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
        d.run("UPDATE agent_req SET status=%s, result=%s, decided=%s, who=%s WHERE id=%s",
              (status, json.dumps(res, ensure_ascii=False, default=str)[:4000], time.time(), who, rid))
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
    with db() as d:
        rid = d.run("INSERT INTO agent_req (at, kind, shop, params, summary, comment, status, result, decided, who) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 'new', '', 0, '')",
                    (time.time(), kind, ch.key, json.dumps(p, ensure_ascii=False), text, str(b.get("comment") or "")[:1000]), ins=True)
    try:
        auto = auto_ok(kind, ch, p)
    except mpk.ApiError:
        auto = False
    if auto:
        status, res = execute(rid, "правило владельца")
        log(200, f"{kind} #{rid} auto {status}")
        return jsonify(ok=True, id=rid, status=status, auto=True, result=res)
    oh.notify_bg(lambda: oh.tg("sendMessage", chat_id=oh.OWNER,
                               text=f"🧑‍💼 Менеджер WB/Ozon просит согласовать (№ {rid}):\n{text}"
                                    + (f"\nКомментарий: {b.get('comment')}" if b.get("comment") else "")
                                    + "\n\nСистема AMURA → Заказы → На согласование"))
    log(200, f"{kind} #{rid}")
    return jsonify(ok=True, id=rid, status="new", auto=False)


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
