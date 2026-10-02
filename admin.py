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
ATTR_HIDDEN, ATTR_PAY, ATTR_BANNERS = "Сайт: скрыт", "Сайт: реквизиты", "Сайт: баннеры"
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


def _all_items():
    v = oh._cache.get("live")
    data = v[1] if v else oh.refresh(oh.LIVE_TTL)
    return data["items"], data.get("hiddenItems", [])


@bp.get("/admin/api/brands")
@guard
def brands():
    shown, hid = _all_items()
    b = {}
    for i in shown + hid:
        r = b.setdefault(i.get("brand") or "Без бренда", {"brand": i.get("brand") or "Без бренда", "total": 0, "hidden": 0})
        r["total"] += 1
    for i in hid:
        b[i.get("brand") or "Без бренда"]["hidden"] += 1
    return jsonify(ok=True, brands=sorted(b.values(), key=lambda r: r["brand"].lower()))


@bp.get("/admin/api/products")
@guard
def products():
    shown, hid = _all_items()
    hidset = {i["id"] for i in hid}
    q = request.args.get("q", "").strip().lower()
    brand = request.args.get("brand", "")
    rows = [i for i in shown + hid
            if (not brand or (i.get("brand") or "Без бренда") == brand)
            and (not q or q in (i["name"] + " " + i.get("brand", "") + " " + i.get("code", "")).lower())]
    rows.sort(key=lambda i: (i["id"] not in hidset, i["name"]))
    size = min(max(int(request.args.get("size", 100) or 100), 10), 200)
    start = max(int(request.args.get("offset", 0) or 0), 0)
    return jsonify(ok=True, total=len(rows), hiddenTotal=len(hid), items=[
        {"id": i["id"], "code": i.get("code", ""), "name": i["name"], "brand": i.get("brand", ""), "qty": i["qty"],
         "rtl": i.get("rtl", 0), "opt": i.get("opt", 0), "hidden": i["id"] in hidset} for i in rows[start:start + size]])


def _set_hidden(ids, val):
    meta = _attr("product", ATTR_HIDDEN, "boolean")["meta"]
    for k in range(0, len(ids), 200):          # МойСклад обновляет пачкой одним запросом
        oh.ms("POST", "/entity/product", json=[{"meta": oh.meta("product", i)["meta"], "attributes": [{"meta": meta, "value": val}]}
                                                for i in ids[k:k + 200]], timeout=40)
    oh._cache.pop("hidden", None)
    oh._cache.pop("live", None)       # каталог пересоберётся с новой видимостью


@bp.post("/admin/api/products/<pid>/hidden")
@guard
def set_hidden(pid):
    if not oh.re.fullmatch(r"[0-9a-f-]{36}", pid):
        return jsonify(ok=False, error="Неверный товар"), 400
    _set_hidden([pid], bool((request.get_json(silent=True) or {}).get("hidden")))
    return jsonify(ok=True)


@bp.post("/admin/api/brands/hidden")
@guard
def set_brand_hidden():
    d = request.get_json(silent=True) or {}
    shown, hid = _all_items()
    brand, val = d.get("brand", ""), bool(d.get("hidden"))
    ids = [i["id"] for i in (shown if val else hid) if (i.get("brand") or "Без бренда") == brand]
    _set_hidden(ids, val)
    return jsonify(ok=True, changed=len(ids))


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


# ---------- баннеры главной ----------
def _clean_banners(d):
    slides = []
    for s in (d.get("slides") or [])[:8]:
        title = str(s.get("title", "")).strip()[:80]
        img = str(s.get("img", "")).strip()[:300]
        if not title and not img:
            continue
        bg = str(s.get("bg", ""))
        out = {"title": title, "text": str(s.get("text", "")).strip()[:200],
               "bg": bg if oh.re.fullmatch(r"#[0-9a-fA-F]{6}", bg) else "#14503C"}
        if oh.re.match(r"https?://", img):
            out["img"] = img
        tags = [str(t).strip()[:24] for t in (s.get("tags") or []) if str(t).strip()][:4]
        if tags:
            out["tags"] = tags
        link = s.get("link") or {}
        for k in ("brand", "q", "url"):
            if str(link.get(k, "")).strip():
                out["link"] = {k: str(link[k]).strip()[:200]}
        if link.get("sort") in ("new",):
            out["link"] = {"sort": "new"}
        if out.get("link") and str(s.get("button", "")).strip():
            out["button"] = str(s["button"]).strip()[:30]
        if s.get("off"):
            out["off"] = True
        slides.append(out)
    return {"autoplaySec": min(max(int(d.get("autoplaySec") or 6), 3), 20), "slides": slides}


def banners_saved():
    org = oh.ms("GET", f"/entity/organization/{oh.organization()}")
    for a in org.get("attributes") or []:
        if a.get("name") == ATTR_BANNERS and a.get("value"):
            try:
                return json.loads(a["value"])
            except Exception:
                return None
    return None


@bp.get("/banners")
def banners_public():
    """Баннеры для сайта; пусто — сайт берёт запасной banners.json."""
    try:
        d = oh.cached("banners", 60, banners_saved)
    except Exception:
        d = None
    if not d:
        return jsonify(error="none"), 404
    resp = jsonify(autoplaySec=d.get("autoplaySec", 6), slides=[s for s in d["slides"] if not s.get("off")])
    resp.headers["Cache-Control"] = "public, max-age=60"
    return oh.cors(resp)


@bp.route("/admin/api/banners", methods=["GET", "PUT"])
@guard
def banners_admin():
    if request.method == "PUT":
        d = _clean_banners(request.get_json(silent=True) or {})
        raw = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
        if len(raw) > 4000:
            return jsonify(ok=False, error="Слишком много текста — сократите баннеры"), 400
        oh.ms("PUT", f"/entity/organization/{oh.organization()}", json={"attributes": [{"meta": _attr("organization", ATTR_BANNERS, "text")["meta"], "value": raw}]})
        oh._cache.pop("banners", None)
        return jsonify(ok=True, **d)
    d = banners_saved()
    if not d:                                  # ещё не сохраняли — берём баннеры, которые сейчас на сайте
        try:
            d = oh.requests.get(f"{oh.SITE_URL}/banners.json", timeout=10).json()
        except Exception:
            d = {"autoplaySec": 6, "slides": []}
    return jsonify(ok=True, saved=bool(banners_saved is not None and d), **_clean_banners(d))
