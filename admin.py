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
import team

bp = Blueprint("admin", __name__)
HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN_HOURS = 24 * 14
ATTR_HIDDEN, ATTR_PAY, ATTR_BANNERS = "Сайт: скрыт", "Сайт: реквизиты", "Сайт: баннеры"
_logins = {}   # nonce -> {"t": время, "ok": bool}


def _sig(body):
    return hmac.new(oh.ORDER_SECRET, b"admin." + body.encode(), hashlib.sha256).hexdigest()[:32]


def make_admin_token(role="owner", chat=None):
    p = {"a": 1, "r": role, "e": int(time.time()) + ADMIN_HOURS * 3600}
    if chat:
        p["c"] = str(chat)
    body = oh._b64(json.dumps(p).encode())
    return body + "." + _sig(body)


def who():
    """Кто вошёл: {"role": "owner"|"staff", "perms": [...], "name": ...} или None. Права сотрудника берутся из базы при каждом запросе —
    отключили человека, и доступ пропал сразу."""
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer ") or not oh.ORDER_SECRET:
        return None
    try:
        body, sig = h[7:].split(".")
        if not hmac.compare_digest(sig, _sig(body)):
            return None
        d = json.loads(oh.base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not d.get("a") or d["e"] <= time.time():
            return None
        if d.get("r", "owner") == "owner":
            return {"role": "owner", "perms": list(team.PERMS), "name": "Владелец"}
        s = team.by_chat(d.get("c"))
        return {"role": "staff", "perms": s["perms"], "name": s["name"]} if s else None
    except Exception:
        return None


def is_admin():
    w = who()
    return bool(w and w["role"] == "owner")


def guard(fn):
    """Только владелец."""
    def w(*a, **k):
        if not is_admin():
            return jsonify(ok=False, error="Войдите заново" if not who() else "Раздел только для владельца"), 401 if not who() else 403
        return fn(*a, **k)
    w.__name__ = fn.__name__
    return w


def need(perm):
    """Владелец или сотрудник с правом perm (inbox / orders / products)."""
    def deco(fn):
        def w(*a, **k):
            me = who()
            if not me:
                return jsonify(ok=False, error="Войдите заново"), 401
            if me["role"] != "owner" and perm not in me["perms"]:
                return jsonify(ok=False, error="Нет доступа к этому разделу"), 403
            return fn(*a, **k)
        w.__name__ = fn.__name__
        return w
    return deco


# ---------- вход ----------
def handle_admin_login(nonce, chat):
    """Вызывается из вебхука бота: /start adm_<код>. Входит владелец или сотрудник, добавленный в «Команда»."""
    v = _logins.get(nonce)
    me = None if str(chat) == str(oh.OWNER) else team.by_chat(chat)
    if str(chat) != str(oh.OWNER) and not me:
        oh.tg("sendMessage", chat_id=chat, text="У вас нет доступа к панели. Попросите владельца добавить вас в разделе «Команда».")
    elif not v:
        oh.tg("sendMessage", chat_id=chat, text="Ссылка устарела, обновите страницу входа.")
    else:
        v.update(ok=True, role="owner" if not me else "staff", chat=str(chat))
        oh.tg("sendMessage", chat_id=chat, text="Вход в панель AMURA подтверждён ✅ Вернитесь в браузер.")


_codes = {}     # ключ входа -> {"code", "t", "tries", "chat", "role"}


def _find_login(username):
    """Кому слать код: пустой username — владельцу, иначе сотруднику с таким @username (или владельцу, если это его @username)."""
    u = (username or "").strip().lstrip("@").lower()
    owner_u = ""
    try:
        owner_u = (oh.tg("getChat", chat_id=oh.OWNER)["result"].get("username") or "").lower() if oh.OWNER else ""
    except Exception:
        pass
    if not u or (owner_u and u == owner_u):
        return ("owner", str(oh.OWNER)) if oh.OWNER else (None, None)
    for st in team.staff():
        if st["active"] and st["username"].lower() == u:
            return "staff", st["chat"]
    return None, None


@bp.post("/admin/api/login/code")
def login_code():
    """Шаг 1: бот присылает одноразовый код в Telegram. Ответ всегда одинаковый, чтобы нельзя было подбирать @username."""
    if oh.too_many("admc:" + oh.client_ip(), limit=6, window=600):
        return jsonify(ok=False, error="Слишком много попыток, подождите 10 минут"), 429
    uname = ((request.get_json(silent=True) or {}).get("username") or "").strip().lstrip("@").lower()
    role, chat = _find_login(uname)
    now = time.time()
    for k in [k for k, v in _codes.items() if now - v["t"] > 300]:
        _codes.pop(k, None)
    if chat and not oh.too_many("admt:" + chat, limit=3, window=600):
        code = f"{secrets.randbelow(900000) + 100000}"
        _codes[uname] = {"code": code, "t": now, "tries": 0, "chat": chat, "role": role}
        try:
            oh.tg("sendMessage", chat_id=chat, text=f"Код входа в панель AMURA: {code}\nДействует 5 минут. Никому его не сообщайте. Если это были не вы — просто проигнорируйте.")
        except Exception as e:
            print("Код входа не отправлен:", e, flush=True)
    return jsonify(ok=True)


@bp.post("/admin/api/login/verify")
def login_verify():
    """Шаг 2: человек вводит код со страницы бота."""
    d = request.get_json(silent=True) or {}
    uname = (d.get("username") or "").strip().lstrip("@").lower()
    v = _codes.get(uname)
    if not v or time.time() - v["t"] > 300 or v["tries"] >= 5:
        _codes.pop(uname, None)
        return jsonify(ok=False, error="Код не подошёл или устарел. Запросите новый."), 400
    v["tries"] += 1
    if not hmac.compare_digest(str(d.get("code", "")).strip(), v["code"]):
        return jsonify(ok=False, error="Неверный код"), 400
    _codes.pop(uname, None)
    return jsonify(ok=True, token=make_admin_token(v["role"], v["chat"]), role=v["role"])


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
        return jsonify(ok=True, token=make_admin_token(v.get("role", "owner"), v.get("chat")))
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
        oh.ms("GET", "/entity/organization", params={"limit": 1}, timeout=6)
    except Exception:
        ms_ok = False
    day = datetime.now(oh.ALMATY).strftime("%Y-%m-%d")
    today = fresh("adm_today", 60, lambda: oh.ms("GET", "/entity/customerorder", params={
        "filter": f"moment>={day} 00:00:00", "limit": 100}, timeout=12)["rows"])
    return jsonify(ok=True, products=len(items), hidden=len(hidden_ids()), ordersToday=len(today),
                   sumToday=sum(o["sum"] for o in today) / 100, catalogUpdated=(v[1]["updated"] if v else ""),
                   siteUpdated=oh._cache.get("site_updated", ""), msOk=ms_ok, msSec=round(time.time() - t0, 1),
                   tgOk=bool(oh.BOT and oh.OWNER), sms=bool(oh.MOBIZON_KEY))


def fresh(key, ttl, fn):
    """Данные из МойСклад с кэшем; если МойСклад завис — отдаём прошлые данные, а не ошибку (и не шлём оповещение)."""
    try:
        return oh.cached(key, ttl, fn)
    except Exception as e:
        print("Панель:", key, e.__class__.__name__, flush=True)
        v = oh._cache.get(key)
        if v:
            return v[1]
        raise Busy()


class Busy(Exception):
    pass


@bp.errorhandler(Busy)
def busy(_):
    return jsonify(ok=False, error="МойСклад долго отвечает — обновите через минуту"), 503


@bp.get("/admin/api/orders")
@need("orders")
def orders():
    rows = fresh("adm_orders", 45, lambda: oh.ms("GET", "/entity/customerorder", params={
        "order": "moment,desc", "limit": 30, "expand": "agent,state"}, timeout=12)["rows"])
    out = []
    for o in rows:
        desc = (o.get("description") or "").split("\n")
        out.append({"number": o["name"], "moment": o["moment"][:16], "client": o["agent"]["name"],
                    "ship": (desc[3][len("Отправка: "):] if len(desc) > 3 and desc[3].startswith("Отправка: ") else ""),
                    "sum": o["sum"] / 100, "state": (o.get("state") or {}).get("name", "Новый"),
                    "pdf": f"{oh.PUBLIC_URL}/order/{o['name']}/pdf?t={oh.sign(o['name'])}"})
    return jsonify(ok=True, orders=out)


def _all_items():
    v = oh._cache.get("live")
    data = v[1] if v else oh.refresh(oh.LIVE_TTL)
    return data["items"], data.get("hiddenItems", [])


@bp.get("/admin/api/brands")
@need("products")
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
@need("products")
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
@need("products")
def set_hidden(pid):
    if not oh.re.fullmatch(r"[0-9a-f-]{36}", pid):
        return jsonify(ok=False, error="Неверный товар"), 400
    _set_hidden([pid], bool((request.get_json(silent=True) or {}).get("hidden")))
    return jsonify(ok=True)


@bp.post("/admin/api/brands/hidden")
@need("products")
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


# ---------- инбокс и ИИ ----------
import inbox  # noqa: E402


@bp.get("/admin/api/inbox/status")
@need("inbox")
def inbox_status():
    with inbox.db() as d:
        return jsonify(ok=True, persistent=inbox.PG, key=bool(inbox.OPENAI_KEY), aiOn=inbox.ai_on(d), model=inbox.OPENAI_MODEL,
                       unread=(d.run("SELECT COALESCE(SUM(unread),0) FROM conv", one=True) or [0])[0])


@bp.get("/admin/api/inbox/convs")
@need("inbox")
def inbox_convs():
    with inbox.db() as d:
        rows = d.run("SELECT id, name, username, status, unread, last_at, last_text FROM conv ORDER BY last_at DESC LIMIT 100", many=True)
    return jsonify(ok=True, convs=[{"id": r[0], "name": r[1], "username": r[2], "status": r[3], "unread": r[4], "at": r[5], "text": r[6]} for r in rows])


@bp.get("/admin/api/inbox/conv/<int:cid>")
@need("inbox")
def inbox_conv(cid):
    with inbox.db() as d:
        c = d.run("SELECT id, name, username, status FROM conv WHERE id=%s", (cid,), one=True)
        if not c:
            return jsonify(ok=False, error="Диалог не найден"), 404
        d.run("UPDATE conv SET unread=0 WHERE id=%s", (cid,))
        ms_ = d.run("SELECT id, role, text, photo, at FROM msg WHERE conv_id=%s ORDER BY id DESC LIMIT 100", (cid,), many=True)[::-1]
    return jsonify(ok=True, conv={"id": c[0], "name": c[1], "username": c[2], "status": c[3]},
                   messages=[{"id": m[0], "role": m[1], "text": m[2], "at": m[4],
                              "photo": (f"/admin/api/inbox/photo/{m[3]}?t={_sig('ph' + m[3])}" if m[3] else "")} for m in ms_])


@bp.post("/admin/api/inbox/conv/<int:cid>/send")
@need("inbox")
def inbox_send(cid):
    text = str((request.get_json(silent=True) or {}).get("text", "")).strip()[:3500]
    if not text:
        return jsonify(ok=False, error="Пустое сообщение"), 400
    with inbox.db() as d:
        c = d.run("SELECT chat_id FROM conv WHERE id=%s", (cid,), one=True)
        if not c:
            return jsonify(ok=False, error="Диалог не найден"), 404
        oh.tg("sendMessage", chat_id=c[0], text=text)
        inbox.save_msg(d, cid, "manager", text)
        d.run("UPDATE conv SET status='manager' WHERE id=%s", (cid,))     # менеджер ответил — ИИ молчит
    return jsonify(ok=True)


@bp.post("/admin/api/inbox/conv/<int:cid>/status")
@need("inbox")
def inbox_set_status(cid):
    st = (request.get_json(silent=True) or {}).get("status")
    if st not in ("ai", "manager", "closed"):
        return jsonify(ok=False, error="Неверный статус"), 400
    with inbox.db() as d:
        d.run("UPDATE conv SET status=%s WHERE id=%s", (st, cid))
    return jsonify(ok=True)


@bp.get("/admin/api/inbox/photo/<path:file_id>")
def inbox_photo(file_id):
    t = request.args.get("t", "")
    if not oh.ORDER_SECRET or not oh.hmac.compare_digest(t, _sig("ph" + file_id)):
        return "", 403
    info = oh.tg("getFile", file_id=file_id)["result"]
    r = oh.requests.get(f"https://api.telegram.org/file/bot{oh.BOT}/{info['file_path']}", timeout=30)
    return oh.Response(r.content, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@bp.route("/admin/api/ai", methods=["GET", "PUT"])
@guard
def ai_settings():
    with inbox.db() as d:
        if request.method == "PUT":
            j = request.get_json(silent=True) or {}
            if "enabled" in j:
                inbox.set_setting(d, "ai_enabled", "1" if j["enabled"] else "0")
            if "rules" in j:
                inbox.set_setting(d, "rules", str(j["rules"]).strip()[:6000] or inbox.DEFAULT_RULES)
            return jsonify(ok=True)
        kb = d.run("SELECT id, title, body FROM kb ORDER BY id", many=True)
        return jsonify(ok=True, enabled=inbox.get_setting(d, "ai_enabled", "1") == "1", key=bool(inbox.OPENAI_KEY), model=inbox.OPENAI_MODEL,
                       persistent=inbox.PG, rules=inbox.get_setting(d, "rules", inbox.DEFAULT_RULES),
                       kb=[{"id": r[0], "title": r[1], "body": r[2]} for r in kb])


@bp.post("/admin/api/ai/kb")
@guard
def kb_save():
    j = request.get_json(silent=True) or {}
    title, body = str(j.get("title", "")).strip()[:120], str(j.get("body", "")).strip()[:4000]
    if not title or not body:
        return jsonify(ok=False, error="Заполните заголовок и текст"), 400
    with inbox.db() as d:
        if j.get("id"):
            d.run("UPDATE kb SET title=%s, body=%s, at=%s WHERE id=%s", (title, body, time.time(), int(j["id"])))
        else:
            d.run("INSERT INTO kb (title, body, at) VALUES (%s,%s,%s)", (title, body, time.time()))
    return jsonify(ok=True)


@bp.delete("/admin/api/ai/kb/<int:kid>")
@guard
def kb_delete(kid):
    with inbox.db() as d:
        d.run("DELETE FROM kb WHERE id=%s", (kid,))
    return jsonify(ok=True)


@bp.post("/admin/api/ai/test")
@guard
def ai_test():
    j = request.get_json(silent=True) or {}
    text = str(j.get("text", "")).strip()[:500]
    hist = [("client" if m.get("role") == "client" else "ai", str(m.get("text", ""))[:500])
            for m in (j.get("history") or [])[-12:] if isinstance(m, dict) and m.get("text")]
    image = str(j.get("image", ""))
    if not image.startswith("data:image/") or len(image) > 3_000_000:
        image = None
    if not text and not image:
        return jsonify(ok=False, error="Напишите вопрос"), 400
    if not inbox.OPENAI_KEY:
        return jsonify(ok=False, error="Ключ OPENAI_API_KEY не добавлен в Render"), 400
    with inbox.db() as d:
        reply, hand = inbox.ai_reply(d, hist, text, image)
        reply = inbox.clean_reply(reply)
    return jsonify(ok=True, reply=reply, handoff=hand)


# ---------- кто я и команда ----------
@bp.get("/admin/api/me")
def me():
    w = who()
    if not w:
        return jsonify(ok=False, error="Войдите заново"), 401
    return jsonify(ok=True, **w)


@bp.get("/admin/api/team")
@guard
def team_list():
    bot = oh.tg("getMe")["result"]["username"] if oh.BOT else ""
    return jsonify(ok=True, staff=team.staff(force=True), invites=team.invites(), perms=team.PERMS, bot=bot, persist=bool(inbox.PG))


@bp.post("/admin/api/team/invite")
@guard
def team_invite():
    d = request.get_json(silent=True) or {}
    code = team.create_invite(d.get("name"), d.get("perms") or [], d.get("notify", True))
    bot = oh.tg("getMe")["result"]["username"]
    return jsonify(ok=True, code=code, link=f"https://t.me/{bot}?start=stf_{code}")


@bp.route("/admin/api/team/<int:sid>", methods=["PATCH", "DELETE"])
@guard
def team_edit(sid):
    if request.method == "DELETE":
        team.remove(sid)
    else:
        team.update(sid, request.get_json(silent=True) or {})
    return jsonify(ok=True)


@bp.delete("/admin/api/team/invite/<code>")
@guard
def team_invite_del(code):
    team.remove_invite(code)
    return jsonify(ok=True)
