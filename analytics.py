"""Модуль «Аналитика» — воронка карточек розничного сайта, как у WB: переходы в карточку → в корзину → заказали.
События шлёт только розничный сайт (amura.kz) пачками раз в несколько секунд: заход, просмотр карточки, добавление в корзину, оформление.
Обезличенно: случайный номер посещения (sessionStorage — живёт до закрытия вкладки), без cookies, без IP, без имени и телефона.
Боты (по User-Agent) не считаются. Храним только счётчики по дням (Алматы): посещения, товар×источник, строки заказов.
Заказали — розничные заказы, созданные в МойСклад (при оплате картой заказ создаётся только после оплаты, т. е. это оплаченные)."""
import json
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

import admin
import inbox
import modules
import order_hook as oh

bp = Blueprint("analytics", __name__)
bp.after_request(oh.cors)                 # сайты amura.kz и opt.amura.kz обращаются к серверу с другого адреса
ALMATY = timezone(timedelta(hours=5))
BOT = re.compile(r"bot|crawl|spider|slurp|headless|lighthouse|preview|facebookexternalhit|whatsapp|telegram|python|curl|wget|httpclient|monitor",
                 re.I)
F_VISIT, F_VIEW, F_CART, F_CHECKOUT = 1, 2, 4, 8
_ready = [False]


def db():
    d = inbox.db()
    if not _ready[0]:
        d.run("CREATE TABLE IF NOT EXISTS an_sess (day TEXT, sid TEXT, src TEXT, f INTEGER, PRIMARY KEY (day, sid))")
        d.run("CREATE TABLE IF NOT EXISTS an_prod (day TEXT, pid TEXT, src TEXT, views INTEGER, carts INTEGER, PRIMARY KEY (day, pid, src))")
        d.run("CREATE TABLE IF NOT EXISTS an_oline (number TEXT, pid TEXT, day TEXT, src TEXT, qty INTEGER, sum DOUBLE PRECISION, PRIMARY KEY (number, pid))")
        d.c.commit()
        _ready[0] = True
    return d


def on():
    return modules.enabled("analytics")


def today():
    return datetime.now(ALMATY).strftime("%Y-%m-%d")


def _src(v):
    return re.sub(r"[^a-z0-9_-]", "", str(v or "").lower())[:40]


def ingest(batch, ua=""):
    """Пачка событий с сайта: {sid, src, ev: [["s"], ["v", id], ["c", id], ["k"]]}. Возвращает число учтённых событий."""
    if not on() or BOT.search(ua or "") or not isinstance(batch, dict):
        return 0
    sid = str(batch.get("sid") or "")
    if not re.fullmatch(r"[a-z0-9]{8,32}", sid):
        return 0
    src, day, f, views, carts = _src(batch.get("src")), today(), 0, {}, {}
    for e in (batch.get("ev") or [])[:200]:
        if not isinstance(e, list) or not e:
            continue
        k = e[0]
        pid = str(e[1])[:40] if len(e) > 1 and re.fullmatch(r"[\w-]{1,40}", str(e[1])) else ""
        if k == "s":
            f |= F_VISIT
        elif k == "v" and pid:
            f |= F_VISIT | F_VIEW
            views[pid] = views.get(pid, 0) + 1
        elif k == "c" and pid:
            f |= F_VISIT | F_CART
            carts[pid] = carts.get(pid, 0) + 1
        elif k == "k":
            f |= F_VISIT | F_CHECKOUT
    if not f:
        return 0
    with db() as d:
        d.run("INSERT INTO an_sess (day, sid, src, f) VALUES (%s, %s, %s, %s) ON CONFLICT (day, sid) DO UPDATE SET f = an_sess.f | excluded.f",
              (day, sid, src, f))
        for pid in set(views) | set(carts):
            d.run("INSERT INTO an_prod (day, pid, src, views, carts) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (day, pid, src) "
                  "DO UPDATE SET views = an_prod.views + excluded.views, carts = an_prod.carts + excluded.carts",
                  (day, pid, src, views.get(pid, 0), carts.get(pid, 0)))
    return sum(views.values()) + sum(carts.values()) + 1


def record_order(number, lines, src):
    """Розничный заказ создан — строки для «Заказали». Повтор номера не удваивает. Сбой не ломает заказ."""
    if not on():
        return
    try:
        with db() as d:
            for l in lines:
                d.run("INSERT INTO an_oline (number, pid, day, src, qty, sum) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (number, pid) DO NOTHING",
                      (str(number), l["id"], today(), _src(src), int(l["qty"]), l["qty"] * l["price"]))
    except Exception as e:
        print("Аналитика: заказ не записан:", e, flush=True)


@bp.route("/an/ev", methods=["POST", "OPTIONS"])
def ev():
    """Сайт шлёт пачку событий (sendBeacon, text/plain — без лишнего запроса CORS). Ответ пустой."""
    if request.method == "OPTIONS":
        return "", 204
    if not on():
        return "", 204
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if oh.too_many("an:" + ip, limit=120, window=600):
        return "", 204
    try:
        batch = json.loads(request.get_data(cache=False, as_text=True)[:20000] or "{}")
    except ValueError:
        return "", 204
    ingest(batch, request.headers.get("User-Agent", ""))
    return "", 204


def report(days=7, src="", now=None):
    """Воронка магазина и товары за период: days=1 — сегодня."""
    end = (now or datetime.now(ALMATY)).astimezone(ALMATY)
    since = (end - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")
    w, p = ("day>=%s", [since]) if not src else ("day>=%s AND src=%s", [since, src])
    with db() as d:
        fs = d.run(f"SELECT f FROM an_sess WHERE {w}", tuple(p), many=True) or []
        prod = d.run(f"SELECT pid, SUM(views), SUM(carts) FROM an_prod WHERE {w} GROUP BY pid", tuple(p), many=True) or []
        ol = d.run(f"SELECT pid, SUM(qty), SUM(sum), COUNT(DISTINCT number) FROM an_oline WHERE {w} GROUP BY pid", tuple(p), many=True) or []
        orders = (d.run(f"SELECT COUNT(DISTINCT number), COALESCE(SUM(sum), 0) FROM an_oline WHERE {w}", tuple(p), one=True) or [0, 0])
        srcs = [r[0] for r in d.run("SELECT DISTINCT src FROM an_sess WHERE day>=%s AND src<>''", (since,), many=True) or []]
    flags = [r[0] or 0 for r in fs]
    funnel = {"visits": sum(1 for x in flags if x & F_VISIT), "viewed": sum(1 for x in flags if x & F_VIEW),
              "carted": sum(1 for x in flags if x & F_CART), "checkout": sum(1 for x in flags if x & F_CHECKOUT),
              "paid": int(orders[0] or 0), "revenue": round(orders[1] or 0)}
    rows = {}
    for pid, v, c in prod:
        rows[pid] = {"id": pid, "views": int(v or 0), "carts": int(c or 0), "qty": 0, "sum": 0, "orders": 0}
    for pid, q, s, n in ol:
        rows.setdefault(pid, {"id": pid, "views": 0, "carts": 0, "qty": 0, "sum": 0, "orders": 0})
        rows[pid].update(qty=int(q or 0), sum=round(s or 0), orders=int(n or 0))
    try:
        cat = oh.panel_catalog()
    except Exception:
        cat = {}
    for r in rows.values():
        it = cat.get(r["id"]) or {}
        name = it.get("name") or (oh._names.get(r["id"]) or ("",))[0] or "Товар снят с сайта"
        r.update(name=name, img=oh.img_url(it.get("img")), brand=it.get("brand") or "",
                 toCart=round(100 * r["carts"] / r["views"], 1) if r["views"] else None,
                 toOrder=round(100 * r["orders"] / r["carts"], 1) if r["carts"] else None)
    return {"since": since, "funnel": funnel, "rows": sorted(rows.values(), key=lambda r: (-r["views"], -r["sum"])), "sources": sorted(srcs)}


@bp.get("/admin/api/analytics")
@admin.guard
def analytics_view():
    if not on():
        return jsonify(ok=False, error="Модуль «Аналитика» выключен"), 404
    try:
        days = int(request.args.get("days", "7"))
    except ValueError:
        days = 7
    r = report(days if days in (1, 7, 30) else 7, _src(request.args.get("src")))
    try:
        import sources
        names = {s["code"]: s["name"] for s in sources.all_sources()} if sources.on() else {}
    except Exception:
        names = {}
    r["sources"] = [{"code": c, "name": names.get(c, c)} for c in r["sources"]]
    return jsonify(ok=True, **r)


def product_report(pid, days=7, now=None):
    """Аналитика одной карточки за период и прошлый такой же период: воронка, по дням, по источникам. Только чтение."""
    end = (now or datetime.now(ALMATY)).astimezone(ALMATY)
    days = max(1, days)
    start = end - timedelta(days=days - 1)
    since, prev = start.strftime("%Y-%m-%d"), (start - timedelta(days=days)).strftime("%Y-%m-%d")
    with db() as d:
        pr = d.run("SELECT day, src, SUM(views), SUM(carts) FROM an_prod WHERE pid=%s AND day>=%s GROUP BY day, src", (pid, prev), many=True) or []
        ol = d.run("SELECT day, src, SUM(qty), SUM(sum), COUNT(DISTINCT number) FROM an_oline WHERE pid=%s AND day>=%s GROUP BY day, src",
                   (pid, prev), many=True) or []
    zero = lambda: {"views": 0, "carts": 0, "qty": 0, "sum": 0, "orders": 0}
    cur, old, daily, srcs = zero(), zero(), {}, {}
    for i in range(days):
        daily[(start + timedelta(days=i)).strftime("%Y-%m-%d")] = zero()
    for day, src, v, c in pr:
        t = cur if day >= since else old
        t["views"] += int(v or 0)
        t["carts"] += int(c or 0)
        if day >= since:
            dd = daily.setdefault(day, zero())
            dd["views"] += int(v or 0)
            dd["carts"] += int(c or 0)
            x = srcs.setdefault(src or "", zero())
            x["views"] += int(v or 0)
            x["carts"] += int(c or 0)
    for day, src, q, sm, n in ol:
        t = cur if day >= since else old
        t["qty"] += int(q or 0)
        t["sum"] += round(sm or 0)
        t["orders"] += int(n or 0)
        if day >= since:
            dd = daily.setdefault(day, zero())
            dd["qty"] += int(q or 0)
            dd["sum"] += round(sm or 0)
            x = srcs.setdefault(src or "", zero())
            x["qty"] += int(q or 0)
            x["sum"] += round(sm or 0)
            x["orders"] += int(n or 0)
    conv = lambda t: {"toCart": round(100 * t["carts"] / t["views"], 1) if t["views"] else None,
                      "toOrder": round(100 * t["orders"] / t["carts"], 1) if t["carts"] else None}
    try:
        it = oh.panel_catalog().get(pid) or {}
    except Exception:
        it = {}
    return {"item": {"id": pid, "name": it.get("name") or (oh._names.get(pid) or ("Товар снят с сайта",))[0], "brand": it.get("brand") or "",
                     "code": it.get("code") or "", "article": it.get("article") or "", "img": oh.img_url(it.get("img"))},
            "cur": cur | conv(cur), "prev": old | conv(old), "since": since,
            "daily": [{"day": k} | v for k, v in sorted(daily.items()) if k >= since],
            "sources": sorted(({"code": k} | v for k, v in srcs.items()), key=lambda x: (-x["views"], -x["sum"]))}


@bp.get("/admin/api/analytics/product/<pid>")
@admin.guard
def analytics_product(pid):
    if not on():
        return jsonify(ok=False, error="Модуль «Аналитика» выключен"), 404
    try:
        days = int(request.args.get("days", "7"))
    except ValueError:
        days = 7
    r = product_report(str(pid)[:40], days if days in (1, 7, 30) else 7)
    try:
        import sources
        names = {s["code"]: s["name"] for s in sources.all_sources()} if sources.on() else {}
    except Exception:
        names = {}
    for x in r["sources"]:
        x["name"] = names.get(x["code"], x["code"]) if x["code"] else "Без источника"
    return jsonify(ok=True, **r)

