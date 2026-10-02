"""Панель управления AMURA (для владельца): /admin.
Вход — через Telegram: бот заказов принимает /start adm_<код> только от OWNER_CHAT_ID.
Настройки хранятся в МойСклад: «Сайт: скрыт» — флаг у товара, «Сайт: реквизиты» — текст у организации."""
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime

from flask import Blueprint, jsonify, request, send_from_directory

import order_hook as oh

bp = Blueprint("admin", __name__)
HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN_HOURS = 24 * 14
ATTR_HIDDEN, ATTR_PAY = "Сайт: скрыт", "Сайт: реквизиты"
_logins = {}   # nonce -> {"t": время, "ok": bool}


def _sig(body):
    return hmac.new(oh.ORDER_SECRET, b"admin." + body.encode(), hashlib.sha256).hexdigest()[:32]


def make_admin_token():
    body = oh._b64(json.dumps({"a": 1, "e": int(time.time()) + ADMIN_HOURS * 3600}).encode())
    return body + "." + _sig(body)


def is_admin():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer ") or not oh.ORDER_SECRET:
        return False
    try:
        body, sig = h[7:].split(".")
        if not hmac.compare_digest(sig, _sig(body)):
            return False
        d = json.loads(oh.base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return bool(d.get("a")) and d["e"] > time.time()
    except Exception:
        return False


def guard(fn):
    def w(*a, **k):
        if not is_admin():
            return jsonify(ok=False, error="Войдите заново"), 401
        return fn(*a, **k)
    w.__name__ = fn.__name__
    return w


# ---------- вход ----------
def handle_admin_login(nonce, chat):
    """Вызывается из вебхука бота: /start adm_<код>."""
    v = _logins.get(nonce)
    if str(chat) != str(oh.OWNER):
        oh.tg("sendMessage", chat_id=chat, text="Эта панель только для владельца.")
    elif not v:
        oh.tg("sendMessage", chat_id=chat, text="Ссылка устарела, обновите страницу входа.")
    else:
        v["ok"] = True
        oh.tg("sendMessage", chat_id=chat, text="Вход в панель AMURA подтверждён ✅ Вернитесь в браузер.")


@bp.post("/admin/api/login")
def login_start():
    if oh.too_many("adm:" + oh.client_ip(), limit=10, window=600):
        return jsonify(ok=False, error="Слишком много попыток"), 429
    now = time.time()
    for k in [k for k, v in _logins.items() if now - v["t"] > 600]:
        _logins.pop(k, None)
    nonce = secrets.token_urlsafe(18).replace("-", "a").replace("_", "b")
    _logins[nonce] = {"t": now, "ok": False}
    me = oh.tg("getMe")["result"]["username"] if oh.BOT else ""
    return jsonify(ok=True, nonce=nonce, link=f"https://t.me/{me}?start=adm_{nonce}")


@bp.get("/admin/api/login/poll")
def login_poll():
    v = _logins.get(request.args.get("nonce", ""))
    if not v:
        return jsonify(ok=False, error="Ссылка устарела"), 404
    if v["ok"]:
        _logins.pop(request.args["nonce"], None)
        return jsonify(ok=True, token=make_admin_token())
    return jsonify(ok=True, token=None)


# ---------- настройки в МойСклад ----------
def _attr(entity, name, typ):
    def load():
        rows = oh.ms("GET", f"/entity/{entity}/metadata/attributes").get("rows", [])
        for r in rows:
            if r["name"] == name:
                return r
        return oh.ms("POST", f"/entity/{entity}/metadata/attributes", json={"name": name, "type": typ, "required": False})
    return oh.cached(f"attr:{entity}:{name}", 86400, load)


def hidden_ids():
    """id скрытых товаров (кэш 60 с); при ошибке МойСклад — последний известный список."""
    try:
        href = _attr("product", ATTR_HIDDEN, "boolean")["meta"]["href"]
        def load():
            ids, off = set(), 0
            while True:
                rows = oh.ms("GET", "/entity/product", params={"filter": f"{href}=true", "limit": 1000, "offset": off}, timeout=12)["rows"]
                ids |= {r["id"] for r in rows}
                if len(rows) < 1000:
                    return ids
                off += 1000
        return oh.cached("hidden", 60, load)
    except Exception as e:
        print("Скрытые товары не получены:", e, flush=True)
        v = oh._cache.get("hidden")
        return v[1] if v else set()


def pay_settings():
    org = oh.ms("GET", f"/entity/organization/{oh.organization()}")
    for a in org.get("attributes") or []:
        if a.get("name") == ATTR_PAY and a.get("value"):
            try:
                return json.loads(a["value"])
            except Exception:
                pass
    return {}


def pay_text_saved():
    """Текст реквизитов, который бот шлёт клиенту; пусто — используется прежний PAY_MESSAGE."""
    try:
        return (oh.cached("paytext", 60, pay_settings) or {}).get("text", "").strip()
    except Exception:
        return ""


# ---------- API ----------
@bp.get("/admin")
def page():
    return send_from_directory(HERE, "admin.html", max_age=0)


@bp.get("/admin/api/overview")
@guard
def overview():
    v = oh._cache.get("live")
    items = v[1]["items"] if v else []
    t0 = time.time()
    ms_ok = True
    try:
        oh.ms("GET", "/entity/organization", params={"limit": 1}, timeout=10)
    except Exception:
        ms_ok = False
    day = datetime.now(oh.ALMATY).strftime("%Y-%m-%d")
    today = oh.ms("GET", "/entity/customerorder", params={"filter": f"moment>={day} 00:00:00", "limit": 100})["rows"]
    return jsonify(ok=True, products=len(items), hidden=len(hidden_ids()), ordersToday=len(today),
                   sumToday=sum(o["sum"] for o in today) / 100, catalogUpdated=(v[1]["updated"] if v else ""),
                   siteUpdated=oh._cache.get("site_updated", ""), msOk=ms_ok, msSec=round(time.time() - t0, 1),
                   tgOk=bool(oh.BOT and oh.OWNER), sms=bool(oh.MOBIZON_KEY))


@bp.get("/admin/api/orders")
@guard
def orders():
    rows = oh.ms("GET", "/entity/customerorder", params={"order": "moment,desc", "limit": 50, "expand": "agent,state"})["rows"]
    out = []
    for o in rows:
        desc = (o.get("description") or "").split("\n")
        out.append({"number": o["name"], "moment": o["moment"][:16], "client": o["agent"]["name"],
                    "ship": (desc[3].replace("Отправка: ", "") if len(desc) > 3 else ""),
                    "sum": o["sum"] / 100, "state": (o.get("state") or {}).get("name", "Новый"),
                    "pdf": f"{oh.PUBLIC_URL}/order/{o['name']}/pdf?t={oh.sign(o['name'])}"})
    return jsonify(ok=True, orders=out)


@bp.get("/admin/api/products")
@guard
def products():
    v = oh._cache.get("live")
    if not v:
        v = (0, oh.refresh(oh.LIVE_TTL))
    hid = hidden_ids()
    q = request.args.get("q", "").strip().lower()
    rows = [i for i in v[1]["items"] if not q or q in (i["name"] + " " + i.get("brand", "") + " " + i.get("code", "")).lower()]
    rows.sort(key=lambda i: (i["id"] not in hid, i["name"]))
    return jsonify(ok=True, total=len(rows), hiddenTotal=len(hid), items=[
        {"id": i["id"], "code": i.get("code", ""), "name": i["name"], "brand": i.get("brand", ""), "qty": i["qty"],
         "rtl": i.get("rtl", 0), "opt": i.get("opt", 0), "mid": i.get("mid", 0), "box": i.get("box", 0),
         "hidden": i["id"] in hid} for i in rows[:200]])


@bp.post("/admin/api/products/<pid>/hidden")
@guard
def set_hidden(pid):
    if not oh.re.fullmatch(r"[0-9a-f-]{36}", pid):
        return jsonify(ok=False, error="Неверный товар"), 400
    val = bool((request.get_json(silent=True) or {}).get("hidden"))
    oh.ms("PUT", f"/entity/product/{pid}", json={"attributes": [{"meta": _attr("product", ATTR_HIDDEN, "boolean")["meta"], "value": val}]})
    oh._cache.pop("hidden", None)
    oh._cache.pop("live", None)       # каталог пересоберётся с новой видимостью
    return jsonify(ok=True)


@bp.route("/admin/api/pay", methods=["GET", "PUT"])
@guard
def pay():
    if request.method == "PUT":
        text = str((request.get_json(silent=True) or {}).get("text", "")).strip()[:3000]
        data = json.dumps({"text": text, "by": "admin", "at": datetime.now(oh.ALMATY).strftime("%d.%m.%Y %H:%M")}, ensure_ascii=False)
        oh.ms("PUT", f"/entity/organization/{oh.organization()}", json={"attributes": [{"meta": _attr("organization", ATTR_PAY, "text")["meta"], "value": data}]})
        oh._cache.pop("paytext", None)
        return jsonify(ok=True)
    s = pay_settings()
    return jsonify(ok=True, text=s.get("text") or oh.pay_text(), at=s.get("at", ""))
