"""Панель управления AMURA (для владельца): /admin.
Вход — через Telegram: бот заказов принимает /start adm_<код> только от OWNER_CHAT_ID.
Настройки хранятся в МойСклад: «Сайт: скрыт» — флаг у товара, «Сайт: реквизиты» — текст у организации."""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, g, has_request_context, jsonify, request, send_from_directory

import ig
import order_hook as oh
import team
import wa

bp = Blueprint("admin", __name__)
HERE = os.path.dirname(os.path.abspath(__file__))


@bp.after_request
def _stale_header(resp):
    if g.get("stale"):
        resp.headers["X-Stale"] = "1"
    return resp
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


_oname = {"t": 0.0, "v": "Владелец"}


def _owner_name():
    """Имя владельца из Telegram (кэш 1 час); без него — «Владелец»."""
    if time.time() - _oname["t"] > 3600 and oh.OWNER and oh.BOT:
        _oname["t"] = time.time()
        try:
            c = oh.tg("getChat", chat_id=oh.OWNER)["result"]
            n = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
            _oname["v"] = n or "Владелец"
        except Exception:
            pass
    return _oname["v"]


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
            return {"role": "owner", "perms": list(team.PERMS), "name": _owner_name(), "chat": str(oh.OWNER)}
        s = team.by_chat(d.get("c"))
        return {"role": "staff", "perms": s["perms"], "name": s["name"], "chat": str(d.get("c"))} if s else None
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
_fails = {}     # chat -> время неверных вводов кода
_locked = {}    # chat -> до какого времени вход по коду закрыт


def _note_fail(chat):
    """Неверный код: после 8 промахов за 30 минут вход по коду для этого аккаунта закрывается на 30 минут, владельцу — оповещение."""
    now = time.time()
    h = [t for t in _fails.get(chat, []) if now - t < 1800] + [now]
    _fails[chat] = h
    if len(h) >= 8:
        _locked[chat] = now + 1800
        _fails.pop(chat, None)
        oh.alert("admlock", "кто-то подбирает код входа в панель — вход по коду закрыт на 30 минут.", every=600)


def _notify_login(role, chat, ip):
    if not oh.notif_on("login"):
        return
    try:
        oh.tg("sendMessage", chat_id=oh.OWNER, text=f"🔐 Вход в панель AMURA ({'владелец' if role == 'owner' else 'сотрудник'}), IP {ip}. Если это не вы — удалите сотрудника в «Команда» или смените токены.")
    except Exception as e:
        print("Оповещение о входе не отправлено:", e, flush=True)


def _find_login(username):
    """Кому слать код: владельцу — только по его @username, сотруднику — по @username из «Команды». Чужой/пустой @username — никому."""
    u = (username or "").strip().lstrip("@").lower()
    owner_u = ""
    try:
        owner_u = (oh.tg("getChat", chat_id=oh.OWNER)["result"].get("username") or "").lower() if oh.OWNER else ""
    except Exception:
        pass
    if oh.OWNER and (u == owner_u if owner_u else not u):      # владелец: по своему @username (без @username у владельца — пустое поле)
        return "owner", str(oh.OWNER)
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
    if chat and _locked.get(chat, 0) > now:
        chat = None
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
    if oh.too_many("admv:" + oh.client_ip(), limit=15, window=600):
        return jsonify(ok=False, error="Слишком много попыток, подождите 10 минут"), 429
    d = request.get_json(silent=True) or {}
    uname = (d.get("username") or "").strip().lstrip("@").lower()
    v = _codes.get(uname)
    if not v or time.time() - v["t"] > 300 or v["tries"] >= 5:
        _codes.pop(uname, None)
        return jsonify(ok=False, error="Код не подошёл или устарел. Запросите новый."), 400
    v["tries"] += 1
    if not hmac.compare_digest(str(d.get("code", "")).strip(), v["code"]):
        _note_fail(v["chat"])
        return jsonify(ok=False, error="Неверный код"), 400
    _codes.pop(uname, None)
    threading.Thread(target=_notify_login, args=(v["role"], v["chat"], oh.client_ip()), daemon=True).start()
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
@bp.get("/admin/manifest.webmanifest")
def panel_manifest():
    """Панель как приложение на экране телефона: своё имя и золотая иконка, чтобы не путать с магазином."""
    return jsonify({"name": "AMURA — панель управления", "short_name": "AMURA Панель", "start_url": "/admin", "scope": "/admin", "display": "standalone",
                    "background_color": "#0E3B2C", "theme_color": "#0E3B2C", "lang": "ru",
                    "icons": [{"src": "/admin/icon/192", "sizes": "192x192", "type": "image/png"},
                              {"src": "/admin/icon/512", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}), 200, {"Content-Type": "application/manifest+json"}


@bp.get("/admin/vendor/<name>")
def panel_vendor(name):
    """pdf.js для просмотра PDF внутри панели (своя копия: не зависим от внешних CDN)."""
    if name not in ("pdf.min.js", "pdf.worker.min.js"):
        return "", 404
    return send_from_directory(os.path.join(HERE, "vendor"), name, max_age=30 * 86400, mimetype="application/javascript")


@bp.get("/admin/sw.js")
def panel_sw():
    """Service worker панели: push-уведомления со звуком и счётчик на иконке."""
    import push
    return push.SW_JS, 200, {"Content-Type": "application/javascript", "Service-Worker-Allowed": "/admin", "Cache-Control": "no-cache"}


@bp.route("/admin/api/notif", methods=["GET", "PUT"])
@guard
def notif():
    """Какие сообщения бот шлёт в Telegram владельцу и сотрудникам."""
    if request.method == "PUT":
        j = request.get_json(silent=True) or {}
        with inbox.db() as d:
            for k in oh.NOTIF:
                if k in j:
                    inbox.set_setting(d, "notif_" + k, "1" if j[k] else "0")
        oh._cache.pop("notif", None)
    st = oh.notif_settings()
    return jsonify(ok=True, items=[{"key": k, "title": t, "on": st.get(k, True)} for k, t in oh.NOTIF.items()])


@bp.get("/admin/api/pulse")
def pulse():
    """Для уведомлений в открытой панели: непрочитанные сообщения и номер последнего заказа."""
    me = who()
    if not me:
        return jsonify(ok=False, error="Войдите заново"), 401
    out = {"ok": True}
    with inbox.db() as d:
        if me["role"] == "owner" or "inbox" in me["perms"]:
            out["unread"] = int((d.run("SELECT COALESCE(SUM(unread),0) FROM conv", one=True) or [0])[0])
        if me["role"] == "owner" or "orders" in me["perms"]:
            out["lastOrder"] = inbox.get_setting(d, "last_order", "")
    return jsonify(out)


@bp.get("/admin/api/push/key")
def push_key():
    if not who():
        return jsonify(ok=False, error="Войдите заново"), 401
    import push
    return jsonify(ok=True, key=push.keys()[1])


@bp.post("/admin/api/push/sub")
def push_sub():
    me = who()
    if not me:
        return jsonify(ok=False, error="Войдите заново"), 401
    import push
    try:
        push.subscribe((request.get_json(silent=True) or {}).get("sub") or {}, me["chat"])
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    if (request.get_json(silent=True) or {}).get("test"):
        push.notify("Уведомления включены ✅", "Так будут приходить новые сообщения и заказы.", "/admin", only_chat=me["chat"])
    return jsonify(ok=True)


@bp.get("/admin/icon/<int:size>")
def panel_icon(size):
    if size not in (180, 192, 512):
        return "", 404
    return send_from_directory(HERE, f"panel-icon-{size}.png", max_age=86400)


@bp.get("/admin/spider.png")
def panel_spider():
    return send_from_directory(HERE, "spider.png", max_age=86400)


@bp.get("/admin")
def page():
    return send_from_directory(HERE, "admin.html", max_age=0)


@bp.get("/admin/api/overview")
@guard
def overview():
    v = oh._cache.get("live")
    items = v[1]["items"] if v else []
    ping = fresh("adm_ping", 30, _ms_ping)
    today = fresh("adm_today", 60, _ms_today)
    return jsonify(ok=True, products=len(items), hidden=len(hidden_ids()), ordersToday=len(today),
                   sumToday=sum(o["sum"] for o in today) / 100, catalogUpdated=(v[1]["updated"] if v else ""),
                   siteUpdated=oh._cache.get("site_updated", ""), msOk=ping["ok"], msSec=ping["sec"],
                   tgOk=bool(oh.BOT and oh.OWNER), sms=bool(oh.MOBIZON_KEY), wa=wa.configured())


MS_TZ = timezone(timedelta(hours=int(os.environ.get("MS_TZ_HOURS", "3"))))   # время в МойСклад — по Москве


def _ms_time(dt):
    return dt.astimezone(MS_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _from_ms(s_):
    return datetime.strptime(s_[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MS_TZ).astimezone(oh.ALMATY)


def _ms_all(path, flt, limit=1000, extra=None):
    out, off = [], 0
    while True:
        r = oh.ms("GET", path, params={"filter": flt, "limit": limit, "offset": off, **(extra or {})}, timeout=40)
        out += r.get("rows", [])
        off += limit
        if off >= (r.get("meta") or {}).get("size", 0) or off >= 10000:
            return out


def _profit_rows(a, b):
    """Отчёт «Прибыльность по товарам» за [a, b) целиком (по 1000 строк)."""
    out, off = [], 0
    while True:
        r = oh.ms("GET", "/report/profit/byproduct", params={"momentFrom": _ms_time(a), "momentTo": _ms_time(b - timedelta(seconds=1)),
                                                              "limit": 1000, "offset": off}, timeout=40)
        out += r.get("rows", [])
        off += 1000
        if off >= (r.get("meta") or {}).get("size", 0) or off >= 20000:
            return out


def _gross(rows):
    """Валовая прибыль = (продажи − себестоимость) − (возвраты − их себестоимость); всё в тенге."""
    sell = sum(r.get("sellSum", 0) for r in rows) / 100
    ret = sum(r.get("returnSum", 0) for r in rows) / 100
    cost = (sum(r.get("sellCostSum", 0) for r in rows) - sum(r.get("returnCostSum", 0) for r in rows)) / 100
    net = sell - ret
    return {"sales": net, "profit": net - cost, "margin": (net - cost) / net * 100 if net else 0}


def _src_of(desc):
    first = (desc or "").split("\n")[0].lower()
    if "ии-продажник" in first:
        return "ИИ-продажник (WhatsApp / Telegram)"
    if "из панели" in first:
        return "Панель (менеджер)"
    if "с сайта" in first:
        return "Сайт"
    return "МойСклад (вручную)"


def _dash_calc(d1, d2, p1=None, p2=None):
    """Сводка за даты [d1..d2] по Алматы и такой же прошлый период: заказы покупателей + чеки кассы."""
    import concurrent.futures as cf
    start = datetime.strptime(d1, "%Y-%m-%d").replace(tzinfo=oh.ALMATY)
    end = datetime.strptime(d2, "%Y-%m-%d").replace(tzinfo=oh.ALMATY) + timedelta(days=1)
    span = end - start
    pstart = start - span
    pend = start
    if p1 and p2:                                  # с чем сравнивать: та же неделя / те же числа прошлого месяца
        pstart = datetime.strptime(p1, "%Y-%m-%d").replace(tzinfo=oh.ALMATY)
        pend = datetime.strptime(p2, "%Y-%m-%d").replace(tzinfo=oh.ALMATY) + timedelta(days=1)
    hourly = span <= timedelta(days=1)
    rng = lambda a, b: f"moment>={_ms_time(a)};moment<{_ms_time(b)}"
    done = lambda a, b: rng(a, b) + ";applicable=true"              # только проведённые — как в МойСклад «Продажи»
    with cf.ThreadPoolExecutor(8) as ex:
        f_o = ex.submit(_ms_all, "/entity/customerorder", rng(start, end))
        f_po = ex.submit(_ms_all, "/entity/customerorder", rng(pstart, pend))
        f_r = ex.submit(_ms_all, "/entity/retaildemand", done(start, end))
        f_pr = ex.submit(_ms_all, "/entity/retaildemand", done(pstart, pend))
        f_d = ex.submit(_ms_all, "/entity/demand", done(start, end))
        f_pd = ex.submit(_ms_all, "/entity/demand", done(pstart, pend))
        f_rt = ex.submit(lambda: _ms_all("/entity/salesreturn", done(start, end)) + _ms_all("/entity/retailsalesreturn", done(start, end)))
        f_prt = ex.submit(lambda: _ms_all("/entity/salesreturn", done(pstart, pend)) + _ms_all("/entity/retailsalesreturn", done(pstart, pend)))
        f_t = ex.submit(_profit_rows, start, end)
        f_pt = ex.submit(_profit_rows, pstart, pend)
        orders, porders, retail, pretail = f_o.result(), f_po.result(), f_r.result(), f_pr.result()
        demands, pdemands, rets, prets = f_d.result(), f_pd.result(), f_rt.result(), f_prt.result()
        try:
            tops = f_t.result()
        except Exception as e:
            print("Дашборд: отчёт по товарам:", e, flush=True)
            tops = None
        try:
            ptops = f_pt.result()
        except Exception as e:
            print("Дашборд: отчёт по товарам (прошлый период):", e, flush=True)
            ptops = None
    gross = _gross(tops) if tops is not None else None
    pgross = _gross(ptops) if ptops is not None else None
    tops = tops or []
    states = {st["meta"]["href"].rsplit("/", 1)[-1]: st for st in fresh("adm_order_states", 600, _ms_states).get("states", [])}
    sid = lambda o: ((o.get("state") or {}).get("meta") or {}).get("href", "").rsplit("/", 1)[-1]
    cancelled = lambda o: "отмен" in (states.get(sid(o), {}).get("name", "")).lower()
    orders = [o for o in orders if not cancelled(o)]
    porders = [o for o in porders if not cancelled(o)]
    n = 24 if hourly else span.days
    bucket = (lambda t: t.hour) if hourly else (lambda t: (t.date() - start.date()).days)
    pbucket = (lambda t: t.hour) if hourly else (lambda t: (t.date() - pstart.date()).days)
    cur, prev, cnt = [0.0] * n, [0.0] * n, [0] * n
    # Выручка = продажи как в МойСклад: проведённые отгрузки + чеки кассы − возвраты
    for o in demands + retail:
        i = bucket(_from_ms(o["moment"]))
        if 0 <= i < n:
            cur[i] += o["sum"] / 100
            cnt[i] += 1
    for o in rets:
        i = bucket(_from_ms(o["moment"]))
        if 0 <= i < n:
            cur[i] -= o["sum"] / 100
    for o in pdemands + pretail:
        i = pbucket(_from_ms(o["moment"]))
        if 0 <= i < n:
            prev[i] += o["sum"] / 100
    for o in prets:
        i = pbucket(_from_ms(o["moment"]))
        if 0 <= i < n:
            prev[i] -= o["sum"] / 100
    src = {}
    for o in orders:
        k = _src_of(o.get("description"))
        a = src.setdefault(k, [0, 0.0])
        a[0] += 1
        a[1] += o["sum"] / 100
    if retail:
        src["Касса"] = [len(retail), sum(r["sum"] for r in retail) / 100]
    st_cnt = {}
    for o in orders:
        nm = states.get(sid(o), {}).get("name", "Без статуса")
        st_cnt[nm] = st_cnt.get(nm, 0) + 1
    order_of = {st.get("name"): i for i, st in enumerate(states.values())}
    agents = {}
    for o in orders:
        aid = ((o.get("agent") or {}).get("meta") or {}).get("href", "").rsplit("/", 1)[-1]
        a = agents.setdefault(aid, [0, 0.0])
        a[0] += 1
        a[1] += o["sum"] / 100
    top_ag = sorted(agents.items(), key=lambda x: -x[1][1])[:5]
    names = {}
    def agname(aid):
        try:
            return aid, oh.ms("GET", f"/entity/counterparty/{aid}", timeout=15).get("name", "")
        except Exception:
            return aid, ""
    with cf.ThreadPoolExecutor(5) as ex:
        names = dict(ex.map(agname, [a for a, _ in top_ag if a]))
    imgs = {}
    try:
        imgs = oh.cached("imgidx", 1800, oh._img_index)
    except Exception:
        pass
    def pid(r):
        return ((r.get("assortment") or {}).get("meta") or {}).get("href", "").rsplit("/", 1)[-1].split("?")[0]
    tops = sorted(tops, key=lambda r: -r.get("sellSum", 0))
    total = lambda L: sum(o["sum"] for o in L) / 100
    rev, prev_rev = total(demands) + total(retail) - total(rets), total(pdemands) + total(pretail) - total(prets)
    cnt_all, pcnt_all = len(demands) + len(retail), len(pdemands) + len(pretail)
    return {
        "from": d1, "to": d2, "hourly": hourly, "pfrom": pstart.strftime("%Y-%m-%d"), "pto": (pend - timedelta(days=1)).strftime("%Y-%m-%d"),
        "revenue": rev, "prevRevenue": prev_rev, "count": cnt_all, "prevCount": pcnt_all,
        "avg": rev / cnt_all if cnt_all else 0, "prevAvg": prev_rev / pcnt_all if pcnt_all else 0,
        "returns": total(rets), "shipped": total(demands), "retail": total(retail), "retailCount": len(retail), "shipCount": len(demands),
        "orders": {"count": len(orders), "sum": total(orders), "prevCount": len(porders), "prevSum": total(porders),
                   "paid": sum(o.get("payedSum", 0) for o in orders) / 100,
                   "unpaid": max(0.0, total(orders) - sum(o.get("payedSum", 0) for o in orders) / 100)},
        "series": cur, "prevSeries": prev, "seriesCount": cnt,
        "sources": sorted([{"name": k, "count": v[0], "sum": v[1]} for k, v in src.items()], key=lambda x: -x["sum"]),
        "states": sorted([{"name": k, "count": v, "color": "#%06x" % states_color(states, k)} for k, v in st_cnt.items()],
                         key=lambda x: order_of.get(x["name"], 99)),
        "topProducts": [{"name": r.get("assortment", {}).get("name", ""), "qty": r.get("sellQuantity", 0), "sum": r.get("sellSum", 0) / 100,
                         "profit": (r.get("sellSum", 0) - r.get("sellCostSum", 0)) / 100,
                         "img": f"{oh.SITE_URL}/img/{pid(r)}.webp" if pid(r) in imgs else ""} for r in tops[:7]],
        "salesTotal": gross["sales"] if gross else 0, "profitTotal": gross["profit"] if gross else 0,
        "gross": gross, "prevGross": pgross,
        "topClients": [{"name": names.get(a, "") or "Клиент", "count": v[0], "sum": v[1]} for a, v in top_ag],
    }


def states_color(states, name):
    for st in states.values():
        if st.get("name") == name:
            return st.get("color") or 0
    return 0


@bp.get("/admin/api/dash")
@guard
def dash():
    today = datetime.now(oh.ALMATY).strftime("%Y-%m-%d")
    d1, d2 = request.args.get("from", today), request.args.get("to", today)
    try:
        a, b = datetime.strptime(d1, "%Y-%m-%d"), datetime.strptime(d2, "%Y-%m-%d")
    except ValueError:
        return jsonify(ok=False, error="Неверные даты"), 400
    if b < a:
        d1, d2 = d2, d1
    if abs((b - a).days) > 400:
        return jsonify(ok=False, error="Период не больше 400 дней"), 400
    p1, p2 = request.args.get("pfrom", ""), request.args.get("pto", "")
    try:
        datetime.strptime(p1, "%Y-%m-%d"), datetime.strptime(p2, "%Y-%m-%d")
    except ValueError:
        p1 = p2 = None
    live = d2 >= today
    try:
        data = oh.cached(f"dash:{d1}:{d2}:{p1}:{p2}", 60 if live else 900, lambda: _dash_calc(d1, d2, p1, p2))
    except Exception as e:
        print("Дашборд:", e, flush=True)
        return jsonify(ok=False, error="МойСклад долго отвечает — повторите через минуту"), 503
    return jsonify(ok=True, today=today, **data)


def _ms_ping():
    t0 = time.time()
    try:
        oh.ms("GET", "/entity/organization", params={"limit": 1}, timeout=6)
        ok = True
    except Exception:
        ok = False
    return {"ok": ok, "sec": round(time.time() - t0, 1)}


def _ms_today():
    day = datetime.now(oh.ALMATY).strftime("%Y-%m-%d")
    return oh.ms("GET", "/entity/customerorder", params={"filter": f"moment>={day} 00:00:00", "limit": 100}, timeout=12)["rows"]


_ord_total = [0]


def _ms_orders():
    r = oh.ms("GET", "/entity/customerorder", params={"order": "moment,desc", "limit": 30, "expand": "agent,state"}, timeout=12)
    _ord_total[0] = (r.get("meta") or {}).get("size", 0)
    return r["rows"]


def _ms_states():
    return oh.ms("GET", "/entity/customerorder/metadata", timeout=12)


_refreshing = set()


def fresh(key, ttl, fn):
    """Данные из МойСклад с кэшем. Устарели — отдаём прошлые сразу, а обновляем в фоне (панель не ждёт МойСклад).
    Данных ещё нет (первый запрос) — ждём; если МойСклад завис, вместо ошибки в панели «обновите через минуту»."""
    v = oh._cache.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    if v:
        if key not in _refreshing:
            _refreshing.add(key)
            threading.Thread(target=_refresh_bg, args=(key, fn), daemon=True).start()
        if has_request_context():
            g.stale = True                             # панель покажет эти данные и сама перезапросит свежие
        return v[1]
    try:
        return oh.cached(key, ttl, fn)
    except Exception as e:
        print("Панель:", key, e.__class__.__name__, flush=True)
        raise Busy()


def _refresh_bg(key, fn):
    try:
        oh._cache[key] = (time.time(), fn())
    except Exception as e:
        print("Панель (фон):", key, e.__class__.__name__, flush=True)
    finally:
        _refreshing.discard(key)


def _warm():
    """После запуска сервера заранее грузим то, что открывает панель: первый вход не ждёт МойСклад."""
    time.sleep(8)
    for key, ttl, fn in (("adm_ping", 30, _ms_ping), ("adm_today", 60, _ms_today), ("adm_orders", 45, _ms_orders), ("adm_order_states", 600, _ms_states)):
        try:
            oh._cache[key] = (time.time(), fn())
        except Exception as e:
            print("Прогрев панели:", key, e.__class__.__name__, flush=True)


if os.environ.get("PORT"):          # только на сервере, не в тестах и скриптах
    threading.Thread(target=_warm, daemon=True).start()


class Busy(Exception):
    pass


@bp.errorhandler(Busy)
def busy(_):
    return jsonify(ok=False, error="МойСклад долго отвечает — обновите через минуту"), 503


# ---------- клиенты: покупатели из МойСклад + переписка + заметки ----------
def _notes_db():
    d = inbox.db()
    d.run("CREATE TABLE IF NOT EXISTS client_note (key TEXT PRIMARY KEY, text TEXT, author TEXT, at DOUBLE PRECISION)")
    return d


_olog_ready = [False]


def _olog(number, text, who_name=None):
    """Журнал панели: кто и что сделал с заказом (в МойСклад все правки панели идут от одного API-пользователя)."""
    try:
        d = inbox.db()
        with d:
            if not _olog_ready[0]:
                d.run("CREATE TABLE IF NOT EXISTS order_log (number TEXT, at DOUBLE PRECISION, who TEXT, text TEXT)")
                d.run("CREATE INDEX IF NOT EXISTS order_log_n ON order_log (number)")
                _olog_ready[0] = True
            d.run("INSERT INTO order_log (number, at, who, text) VALUES (%s,%s,%s,%s)",
                  (str(number), time.time(), who_name or (who() or {}).get("name", "") or "Панель", text[:3000]))
    except Exception as e:
        print("Журнал заказа не записан:", e, flush=True)


def _cp_row(c):
    tgid = next((str(a.get("value") or "") for a in c.get("attributes") or [] if a.get("name") == oh.ATTR_TGID), "")
    return {"id": c["id"], "name": c.get("name", ""), "phone": c.get("phone", ""), "email": c.get("email", ""),
            "tags": c.get("tags") or [], "tg": tgid, "city": c.get("actualAddress", ""), "created": (c.get("created") or "")[:10]}


def _chats_for(phone, tgid):
    """Диалоги клиента в «Сообщениях»: WhatsApp — по номеру, Telegram — по Telegram ID из МойСклад."""
    keys = []
    ph = oh.norm_phone(phone)
    if ph:
        keys.append("wa:" + ph)
    if tgid:
        keys.append(str(tgid))
    if not keys:
        return []
    with inbox.db() as d:
        rows = d.run("SELECT id, chat_id, last_at, last_text, unread FROM conv WHERE chat_id IN (" + ",".join(["%s"] * len(keys)) + ")", tuple(keys), many=True)
    return [{"id": r[0], "channel": inbox.channel(r[1]), "at": r[2], "text": r[3], "unread": r[4]} for r in rows]


@bp.get("/admin/api/clients")
@need("orders")
def clients():
    """Список: src=ms — покупатели из МойСклад (последние изменённые сверху, поиск по имени/телефону);
    src=retail — только покупатели розничного сайта;
    src=chat — все, кто писал в Telegram/WhatsApp."""
    q, page = str(request.args.get("q", "")).strip()[:60], max(0, int(request.args.get("page", 0) or 0))
    if request.args.get("src") == "chat":
        with inbox.db() as d:
            rows = d.run("SELECT id, name, username, chat_id, last_at, last_text, unread FROM conv WHERE last_at IS NULL OR last_at>0 ORDER BY last_at DESC LIMIT 300", many=True)
        out = [{"conv": r[0], "name": r[1], "username": r[2], "channel": inbox.channel(r[3]),
                "phone": wa.number(r[3]) if wa.is_wa(r[3]) else "", "at": r[4], "text": r[5], "unread": r[6]} for r in rows]
        if q:
            ql = q.lower()
            out = [x for x in out if ql in (x["name"] + " " + x["username"] + " " + x["phone"]).lower()]
        return jsonify(ok=True, clients=out[:100], more=False)
    params = {"limit": 50, "offset": page * 50, "order": "updated,desc"}
    if q:
        params["search"] = q
    retail = request.args.get("src") == "retail"           # покупатели розничного сайта (метка «розница»)
    if retail:
        params["filter"] = f"tags={oh.RETAIL_TAG}"
    rows = fresh(f"adm_cl:{'r' if retail else ''}:{q}:{page}", 60, lambda: oh.ms("GET", "/entity/counterparty", params=params, timeout=12)["rows"])
    return jsonify(ok=True, clients=[_cp_row(c) for c in rows], more=len(rows) == 50)


@bp.get("/admin/api/clients/<cid>")
@need("orders")
def client_card(cid):
    """Карточка: контакты, заказы и сумма покупок, долг/переплата (МойСклад), переписка, заметки.
    cid — id контрагента в МойСклад или «conv<номер>» для клиента, который только писал в мессенджер."""
    conv, cp = None, None
    if cid.startswith("conv"):
        with inbox.db() as d:
            conv = d.run("SELECT id, name, username, chat_id FROM conv WHERE id=%s", (int(cid[4:] or 0),), one=True)
        if not conv:
            return jsonify(ok=False, error="Клиент не найден"), 404
        try:                                        # ищем такого же покупателя в МойСклад
            if wa.is_wa(conv[3]):
                found = oh.ms("GET", "/entity/counterparty", params={"search": wa.number(conv[3])[-10:], "limit": 1}, timeout=10)["rows"]
            elif ig.is_ig(conv[3]):                 # у Instagram нет ни номера, ни Telegram ID — искать в МойСклад не по чему
                found = []
            else:
                found = oh.ms("GET", "/entity/counterparty", params={"filter": f"{oh.tg_attr()['meta']['href']}={conv[3]}", "limit": 1}, timeout=10)["rows"]
            cp = found[0] if found else None
        except Exception as e:
            print("Клиент: поиск в МойСклад:", e, flush=True)
    else:
        if not re.fullmatch(r"[0-9a-f-]{36}", cid):
            return jsonify(ok=False, error="Клиент не найден"), 404
        cp = oh.ms("GET", f"/entity/counterparty/{cid}", timeout=12)
    info = _cp_row(cp) if cp else {"id": "", "name": conv[1], "phone": wa.number(conv[3]) if wa.is_wa(conv[3]) else "",
                                   "email": "", "tags": [], "tg": conv[3] if inbox.channel(conv[3]) == "tg" else "", "city": "", "created": ""}
    if conv and not cp:
        info["username"] = conv[2]
    orders, total, paid, balance = [], 0.0, 0.0, None
    if cp:
        rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"agent={oh.API}/entity/counterparty/{cp['id']}",
                                                            "order": "moment,desc", "limit": 50, "expand": "state"}, timeout=12)["rows"]
        for o in rows:
            st = o.get("state") or {}
            cancelled = bool(re.search(r"отмен", st.get("name", ""), re.I))
            if not cancelled:
                total += o["sum"] / 100
                paid += o.get("payedSum", 0) / 100
            orders.append({"number": o["name"], "id": o["id"], "date": o["moment"][:10], "sum": o["sum"] / 100, "paid": o.get("payedSum", 0) / 100,
                           "state": st.get("name", "Новый"), "color": "#%06x" % st["color"] if st.get("color") else "",
                           "pdf": f"{oh.PUBLIC_URL}/order/{o['name']}/pdf?t={oh.sign(o['name'])}"})
        try:                                        # баланс взаиморасчётов из МойСклад: минус — клиент должен, плюс — переплата
            balance = oh.ms("GET", f"/report/counterparty/{cp['id']}", timeout=10).get("balance", 0) / 100
        except Exception as e:
            print("Клиент: баланс не получен:", e, flush=True)
    chats = _chats_for(info["phone"], info["tg"])
    if conv and not any(c["id"] == conv[0] for c in chats):
        chats.append({"id": conv[0], "channel": inbox.channel(conv[3]), "at": 0, "text": "", "unread": 0})
    keys = [k for k in (("ms:" + cp["id"]) if cp else "", ("conv:" + str(conv[0])) if conv else "") if k]
    with _notes_db() as d:
        note = next((r for r in (d.run("SELECT text, author, at FROM client_note WHERE key=%s", (k,), one=True) for k in keys) if r), None)
    return jsonify(ok=True, client=info, orders=orders, stats={"count": len([o for o in orders if not re.search(r"отмен", o["state"], re.I)]),
                   "total": total, "paid": paid, "unpaid": max(0.0, total - paid), "balance": balance},
                   chats=chats, note={"key": keys[0], "text": note[0] if note else "", "author": note[1] if note else "", "at": note[2] if note else 0})


@bp.post("/admin/api/clients/note")
@need("orders")
def client_note():
    d_ = request.get_json(silent=True) or {}
    key, text = str(d_.get("key", ""))[:60], str(d_.get("text", "")).strip()[:4000]
    if not re.fullmatch(r"(ms:[0-9a-f-]{36}|conv:\d+)", key):
        return jsonify(ok=False, error="Неизвестный клиент"), 400
    with _notes_db() as d:
        d.run("DELETE FROM client_note WHERE key=%s", (key,))
        if text:
            d.run("INSERT INTO client_note (key, text, author, at) VALUES (%s,%s,%s,%s)", (key, text, (who() or {}).get("name", ""), time.time()))
    return jsonify(ok=True)


@bp.get("/admin/api/orders")
@need("orders")
def orders():
    q = request.args.get("q", "").strip()[:80]
    retail = request.args.get("kind") == "retail"  # вкладка «Розница»: только заказы с розничного сайта
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        offset = 0
    if q or offset or retail:                      # поиск по всем заказам (номер, контрагент, телефон, город) и «Показать ещё»
        params = {"order": "moment,desc", "limit": 50, "offset": offset, "expand": "agent,state"}
        if q:
            params["search"] = q
        if retail:
            params["filter"] = "description~" + oh.RETAIL_MARK
        try:
            r = oh.ms("GET", "/entity/customerorder", params=params, timeout=20)
        except Exception as e:
            print("Заказы (поиск / ещё):", e, flush=True)
            return jsonify(ok=False, error="МойСклад долго отвечает — повторите через минуту"), 503
        rows, total = r["rows"], (r.get("meta") or {}).get("size", 0)
        if q and not offset:
            rows.sort(key=lambda o: o.get("name", "").lstrip("0") != q.lstrip("0"))    # точный номер — первым
    else:
        rows = fresh("adm_orders", 45, _ms_orders)
        total = _ord_total[0] or len(rows)
        oh.pdf_prepare([(o["name"], o["id"], o.get("updated")) for o in rows[:30]])
    out = []
    for o in rows:
        desc = (o.get("description") or "").split("\n")
        out.append({"id": o["id"], "number": o["name"], "moment": o["moment"][:16], "client": o["agent"]["name"],
                    "retail": (o.get("description") or "").startswith(oh.RETAIL_MARK),
                    "ship": (desc[3][len("Отправка: "):] if len(desc) > 3 and desc[3].startswith("Отправка: ") else ""),
                    "sum": o["sum"] / 100, "state": (o.get("state") or {}).get("name", "Новый"),
                    "color": "#%06x" % ((o.get("state") or {}).get("color") or 0) if (o.get("state") or {}).get("color") else "",
                    "pdf": f"{oh.PUBLIC_URL}/order/{o['name']}/pdf?t={oh.sign(o['name'])}"})
    nxt = offset + len(rows)
    return jsonify(ok=True, orders=out, states=_order_states(), can_edit=True, total=total, next=nxt, more=nxt < total)


def _order_states():
    md = fresh("adm_order_states", 600, _ms_states)
    return [{"name": s["name"], "color": "#%06x" % s["color"] if s.get("color") else ""} for s in md.get("states", [])]


@bp.post("/admin/api/orders/state")
@need("orders")
def order_state():
    """Сменить статус заказа в МойСклад (тот же, что в самом МойСклад)."""
    b = request.get_json(silent=True) or {}
    num, name = str(b.get("number", "")).strip(), str(b.get("state", "")).strip()
    if not num or not name:
        return jsonify(ok=False, error="Не указан заказ или статус"), 400
    try:
        md = fresh("adm_order_states", 600, _ms_states)
        st = next((s for s in md.get("states", []) if s["name"] == name), None)
        if not st:
            return jsonify(ok=False, error="Такого статуса нет в МойСклад"), 400
        oid = str(b.get("id", ""))
        if not oh.re.fullmatch(r"[0-9a-f-]{36}", oid):
            rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"name={num}", "limit": 1}, timeout=30).get("rows", [])
            if not rows:
                return jsonify(ok=False, error="Заказ не найден в МойСклад"), 404
            oid = rows[0]["id"]
        oh.ms("PUT", f"/entity/customerorder/{oid}", json={"state": {"meta": st["meta"]}}, timeout=30)
        _olog(num, f"Статус → «{name}»")
        if name == "Собран":                      # Express: собрали — курьер вызывается сам (Обзор → Доставка → автовызов)
            import courier
            threading.Thread(target=courier.auto_express, args=(num, oid), daemon=True, name="xauto-" + num).start()
    except Busy:
        return jsonify(ok=False, error="МойСклад долго отвечает — повторите через минуту"), 503
    except Exception as e:
        return jsonify(ok=False, error="Не удалось сменить статус: " + str(e)[:200]), 502
    oh._cache.pop("adm_orders", None)
    return jsonify(ok=True)


def _order_fetch(number):
    """Заказ и его позиции. Если в запросе есть id (из списка), поиск по номеру не нужен — это быстрее."""
    oid = request.args.get("id", "")
    if oh.re.fullmatch(r"[0-9a-f-]{36}", oid):
        o = oh.ms("GET", f"/entity/customerorder/{oid}", params={"expand": "agent,state"}, timeout=30)
        if o.get("name") != number:
            return None, None
    else:
        rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"name={number}", "limit": 1, "expand": "agent,state"}, timeout=30).get("rows", [])
        if not rows:
            return None, None
        o = rows[0]
    return o, oh.order_positions(o["id"])


@bp.get("/admin/api/orders/<number>")
@need("orders")
def order_detail(number):
    try:
        o, pos = _order_fetch(number)
    except Exception as e:
        return jsonify(ok=False, error="МойСклад не ответил: " + str(e)[:150]), 502
    if not o:
        return jsonify(ok=False, error="Заказ не найден"), 404
    lines, loader, fee = [], 0, 0
    try:
        imgs = oh.cached("imgidx", 1800, oh._img_index)
    except Exception:
        imgs = {}
    for p in pos:
        a, price, qty = p["assortment"], p["price"] / 100, p["quantity"]
        if a.get("code") == oh.LOADER_CODE:
            loader += price * qty
        elif a["meta"]["type"] == "service" and a.get("name") == oh.FEE_NAME:
            fee += price * qty
        else:
            lines.append({"pos": p["id"], "type": a["meta"]["type"], "id": a["id"], "name": a["name"], "code": a.get("code", ""),
                          "qty": int(qty), "price": price, "img": f"{oh.SITE_URL}/img/{a['id']}.webp" if a["id"] in imgs else "",
                          "reserve": int(p.get("reserve") or 0), "shipped": int(p.get("shipped") or 0)})
    sig = oh.sign(o["name"])
    return jsonify(ok=True, number=o["name"], id=o["id"], moment=o.get("moment", "")[:16],
                   pdf=f"{oh.PUBLIC_URL}/order/{o['name']}/pdf?t={sig}", xlsx=f"{oh.PUBLIC_URL}/order/{o['name']}/xlsx?t={sig}&id={o['id']}", client=o["agent"]["name"], state=(o.get("state") or {}).get("name", ""), states=_order_states(),
                   description=o.get("description") or "", lines=lines, loader=loader, fee=fee, total=o["sum"] / 100,
                   feeRate=oh.FEE_RATE, hasFee=any(p["assortment"].get("name") == oh.FEE_NAME for p in pos),
                   store=(o.get("store") or {}).get("meta", {}).get("href", "").rstrip("/").rsplit("/", 1)[-1],
                   reserved=any((l["reserve"] or 0) > 0 for l in lines if l["type"] in RESERVABLE))


@bp.post("/admin/api/orders/bulk")
@need("orders")
def orders_bulk():
    """Массово по выбранным заказам: сменить статус ({"action": "state", "state": "Собран"}) или удалить ({"action": "delete"}).
    Удаляет только владелец; заказ с действующей заявкой курьера Яндекса не удаляется — сначала отмените заявку."""
    b = request.get_json(silent=True) or {}
    act = b.get("action")
    items = [x for x in (b.get("orders") or []) if isinstance(x, dict) and x.get("number")][:100]
    if not items or act not in ("state", "delete"):
        return jsonify(ok=False, error="Не выбраны заказы или действие"), 400
    if act == "delete" and (who() or {}).get("role") != "owner":
        return jsonify(ok=False, error="Удалять заказы может только владелец"), 403
    st = None
    if act == "state":
        name = str(b.get("state", "")).strip()
        md = fresh("adm_order_states", 600, _ms_states)
        st = next((x for x in md.get("states", []) if x["name"] == name), None)
        if not st:
            return jsonify(ok=False, error="Такого статуса нет в МойСклад"), 400
    import courier
    res = {}
    for x in items:
        num = str(x["number"])[:20]
        try:
            oid = str(x.get("id") or "")
            if not oh.re.fullmatch(r"[0-9a-f-]{36}", oid):
                rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"name={num}", "limit": 1}, timeout=30).get("rows", [])
                if not rows:
                    res[num] = "не найден"
                    continue
                oid = rows[0]["id"]
            if act == "state":
                oh.ms("PUT", f"/entity/customerorder/{oid}", json={"state": {"meta": st["meta"]}}, timeout=30)
                _olog(num, f"Статус → «{st['name']}» (массово)")
                if st["name"] == "Собран":
                    threading.Thread(target=courier.auto_express, args=(num, oid), daemon=True, name="xauto-" + num).start()
            else:
                with courier.db() as d:
                    row = courier._row(d, num)
                if row and row["claim"] and row["status"] not in courier.DONE and row["status"] not in ("new", "estimating", "ready_for_approval"):
                    res[num] = "у заказа едет курьер Яндекса — сначала отмените заявку"
                    continue
                oh.ms("DELETE", f"/entity/customerorder/{oid}", timeout=30)
                _olog(num, "Заказ удалён из панели")
            res[num] = "ok"
        except Busy:
            res[num] = "МойСклад долго отвечает — повторите"
        except Exception as e:
            msg = str(e)
            res[num] = "есть связанные документы (отгрузка, платёж) — удалите их в МойСклад или поставьте статус «Отменен»" if "связ" in msg.lower() or "1052" in msg or "linked" in msg.lower() else msg[:150]
    oh._cache.pop("adm_orders", None)
    return jsonify(ok=True, result=res)


@bp.post("/admin/api/orders/assembly")
@need("orders")
def orders_assembly():
    """Лист сборки: по выбранным заказам — товары всех заказов (сумма штук) и состав каждого заказа."""
    want = [x for x in ((request.get_json(silent=True) or {}).get("orders") or []) if isinstance(x, dict)][:60]
    def one(x):
        oid, num = str(x.get("id") or ""), str(x.get("number") or "")[:20]
        if oh.re.fullmatch(r"[0-9a-f-]{36}", oid):
            o = oh.ms("GET", f"/entity/customerorder/{oid}", params={"expand": "agent"}, timeout=30)
        else:
            rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"name={num}", "limit": 1, "expand": "agent"}, timeout=30).get("rows", [])
            o = rows[0] if rows else None
        if not o:
            return {"number": num, "error": "не найден"}
        lines = [{"id": p["assortment"]["id"], "name": p["assortment"].get("name", ""), "code": p["assortment"].get("code", ""), "qty": int(p["quantity"])}
                 for p in oh.order_positions(o["id"]) if p["assortment"]["meta"]["type"] in RESERVABLE]
        desc = (o.get("description") or "").split("\n")
        ship = next((l[len("Отправка: "):] for l in desc if l.startswith("Отправка: ")), "")
        return {"number": o["name"], "client": (o.get("agent") or {}).get("name", ""), "ship": ship, "lines": lines}
    from concurrent.futures import ThreadPoolExecutor
    try:
        with ThreadPoolExecutor(4) as ex:                  # МойСклад: не больше 5 запросов одновременно
            res = list(ex.map(one, want))
    except Exception as e:
        return jsonify(ok=False, error="МойСклад не ответил: " + str(e)[:150]), 502
    return jsonify(ok=True, orders=res)


# ---------- наклейки на пакеты: PDF точного размера (термопринтер / A4), телефон печатает его через «Поделиться» ----------
LABEL_SIZES = {"t58": (58, 40), "t75": (75, 120), "a4": (210, 297)}
_labels = {}                       # токен → (время, pdf); воркер один (gunicorn --workers 1), живут 30 минут


def labels_pdf(items, fmt):
    import io
    from reportlab.lib.units import mm
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas
    W, H = LABEL_SIZES.get(fmt, LABEL_SIZES["a4"])
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W * mm, H * mm))
    c.setTitle("Наклейки AMURA")
    small = fmt == "t58"
    lw, lh = (105, 74.25) if fmt == "a4" else (W, H)
    pad = (3 if small else 6) * mm
    for i, x in enumerate(items):
        if fmt == "a4":
            k = i % 8
            if i and not k:
                c.showPage()
            ox, oy = (k % 2) * lw * mm, H * mm - (k // 2 + 1) * lh * mm
            c.setDash(1, 2)
            c.setLineWidth(0.3)
            c.setStrokeGray(0.6)
            c.rect(ox, oy, lw * mm, lh * mm)
            c.setDash()
        else:
            if i:
                c.showPage()
            ox, oy = 0, 0
        left, top, width = ox + pad, oy + lh * mm - pad, lw * mm - 2 * pad
        y = top
        fs = 7 if small else 10
        c.setFillGray(0)
        c.setFont("DVB", fs)
        c.drawString(left, y - fs, "AMURA")
        kind = str(x.get("kind") or "")[:20]
        if kind:
            kw = c.stringWidth(kind, "DVB", fs) + 3 * mm
            c.setStrokeGray(0)
            c.setLineWidth(0.6)
            c.rect(left + width - kw, y - fs - 1.2 * mm, kw, fs + 1.8 * mm)
            c.drawString(left + width - kw + 1.5 * mm, y - fs, kind)
        y -= fs + (2 if small else 4) * mm
        big = 20 if small else 40 if fmt == "t75" else 32
        c.setFont("DVB", big)
        c.drawString(left, y - big * 0.8, "№" + str(x.get("num") or "")[:20])
        y -= big * 0.8 + (1.5 if small else 3) * mm
        for text, font, size, maxl in ((x.get("name"), "DVB", 8.5 if small else 13, 1), (x.get("addr"), "DV", 7.5 if small else 11, 2 if small else 4)):
            for line in simpleSplit(str(text or ""), font, size, width)[:maxl]:
                c.setFont(font, size)
                c.drawString(left, y - size, line)
                y -= size * 1.2
            y -= 1 * mm
        foot = " · ".join(v for v in (str(x.get("slot") or ""), ("тел. …" + str(x.get("phone"))[-4:]) if x.get("phone") else "") if v)
        if foot and not small:
            c.setFont("DV", 9 if fmt == "a4" else 10)
            for j, line in enumerate(reversed(simpleSplit(foot, "DV", 10, width)[:2])):
                c.drawString(left, oy + pad + j * 12, line)
    c.save()
    return buf.getvalue()


@bp.post("/admin/api/labels")
@need("orders")
def labels_make():
    b = request.get_json(silent=True) or {}
    items = [x for x in (b.get("items") or []) if isinstance(x, dict)][:200]
    if not items:
        return jsonify(ok=False, error="Нет заказов для наклеек"), 400
    fmt = b.get("fmt") if b.get("fmt") in LABEL_SIZES else "a4"
    now = time.time()
    for k in [k for k, v in _labels.items() if now - v[0] > 1800]:
        _labels.pop(k, None)
    tok = secrets.token_urlsafe(18)
    _labels[tok] = (now, labels_pdf(items, fmt))
    return jsonify(ok=True, url=f"/labels/{tok}.pdf")


@bp.get("/labels/<tok>.pdf")
def labels_get(tok):
    from flask import Response
    v = _labels.get(tok)
    if not v:
        return "Наклейки устарели — нажмите «Печать наклеек» ещё раз", 404
    return Response(v[1], mimetype="application/pdf", headers={"Content-Disposition": "inline; filename=AMURA-labels.pdf", "Cache-Control": "no-store"})


RESERVABLE = ("product", "variant", "bundle")       # резервируются товары, модификации и комплекты (не услуги)


def _stock_by(kind, store):
    """Остатки МойСклад одного вида (stock / reserve) по товарам — на складе заказа или по всем складам."""
    if not store:
        data = oh.ms("GET", "/report/stock/all/current", params={"stockType": kind}, timeout=20)
        rows = data if isinstance(data, list) else data.get("rows", [])
        return {r["assortmentId"]: r.get(kind) or 0 for r in rows}
    data = oh.ms("GET", "/report/stock/bystore/current", params={"stockType": kind}, timeout=20)
    rows = data if isinstance(data, list) else data.get("rows", [])
    return {r["assortmentId"]: r.get(kind) or 0 for r in rows if r.get("storeId") == store}


@bp.get("/admin/api/orders/<number>/stock")
@need("orders")
def order_stock(number):
    """Остаток, резерв и доступно по позициям заказа — как колонки в МойСклад (на складе заказа)."""
    store = str(request.args.get("store", ""))
    store = store if oh.re.fullmatch(r"[0-9a-f-]{36}", store) else ""
    ids = [i for i in str(request.args.get("ids", "")).split(",") if oh.re.fullmatch(r"[0-9a-f-]{36}", i)][:1000]
    try:
        st = oh.cached(f"ostk:{store}:stock", 60, lambda: _stock_by("stock", store))
        rs = oh.cached(f"ostk:{store}:reserve", 60, lambda: _stock_by("reserve", store))
    except Exception as e:
        return jsonify(ok=False, error="МойСклад не ответил: " + str(e)[:150]), 502
    return jsonify(ok=True, items={i: {"stock": st.get(i, 0), "reserve": rs.get(i, 0), "free": st.get(i, 0) - rs.get(i, 0)} for i in ids})


@bp.post("/admin/api/orders/<number>/reserve")
@need("orders")
def order_reserve(number):
    """Поставить или снять резерв по всем товарам заказа."""
    on = bool((request.get_json(silent=True) or {}).get("on"))
    try:
        o, pos = _order_fetch(number)
        if not o:
            return jsonify(ok=False, error="Заказ не найден"), 404
        if on and not o.get("store"):
            return jsonify(ok=False, error="У заказа не указан склад — резерв поставить нельзя"), 400
        positions = []
        for p in pos:
            a = p["assortment"]
            e = {"id": p["id"], "quantity": p["quantity"], "price": p["price"], "assortment": {"meta": a["meta"]}}
            for k in ("discount", "vat", "vatEnabled"):
                if k in p:
                    e[k] = p[k]
            if a["meta"]["type"] in RESERVABLE:
                e["reserve"] = p["quantity"] if on else 0
            positions.append(e)
        oh.ms("PUT", f"/entity/customerorder/{o['id']}", json={"positions": positions}, timeout=55)
    except Exception as e:
        return jsonify(ok=False, error="Не удалось: " + str(e)[:200]), 502
    _olog(number, "Резерв поставлен" if on else "Резерв снят")
    for k in [k for k in oh._cache if k.startswith("ostk:")]:
        oh._cache.pop(k, None)
    return jsonify(ok=True, reserved=on)


@bp.post("/admin/api/orders/new")
@need("orders")
def order_new():
    """Новый заказ из панели: тот же путь, что и с сайта (контрагент по телефону, резерв, комиссия, грузчик, PDF)."""
    d = request.get_json(silent=True) or {}
    key = "panel:" + (str(d.get("orderKey") or "")[:64] or secrets.token_hex(8))
    hit = oh._orders.get(key)                       # двойное нажатие «Создать» не создаёт второй заказ
    if hit:
        return jsonify(**hit[1])
    me = who() or {}
    payload, status = oh.order_core(d, key, "", None, source=f"из панели ({me.get('name') or 'менеджер'})", panel=True)
    data = payload.pop("_data", None)
    if status != 200:
        return jsonify(**payload), status
    asked = {str(p.get("id")): int(p.get("qty") or 0) for p in (d.get("items") or [])}
    got = {l["id"]: l["qty"] for l in (data or {}).get("lines", [])}
    names = {x["id"]: x["name"] for x in _all_items()[0]}
    short = [names.get(i, "товар") + (f" — {got[i]} из {n} шт." if i in got else " — нет в наличии")
             for i, n in asked.items() if got.get(i, 0) < n]
    out = dict(ok=True, number=payload["number"], total=payload["total"], short=short)
    _olog(payload["number"], f"Создан в панели: {len(got)} поз. на {payload['total']:,.0f} ₸".replace(",", " "), me.get("name"))
    oh._orders[key] = (time.time(), out)
    oh._cache.pop("adm_orders", None)
    return jsonify(**out)


@bp.get("/admin/api/orders/ship")
@need("orders")
def order_ship():
    return jsonify(ok=True, ship=[{"key": k, "name": v[0], "loader": v[1]} for k, v in oh.SHIPPING.items()],
                   loader=oh.LOADER_PRICE, feeRate=oh.FEE_RATE)


_RU, _EN = "йцукенгшщзхъфывапролджэячсмитьбюё", "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
_RU2EN, _EN2RU = str.maketrans(_RU, _EN), str.maketrans(_EN, _RU)


def smart_find(items, q, limit=30):
    """Поиск товара как в кассе МойСклад: куски слов в любом порядке («dark sun», «сел санскр»), код,
    и набор в неправильной раскладке («вфкл ыгт» = «dark sun»)."""
    q = q.lower().replace("ё", "е").strip()
    toks = [t for t in re.split(r"\s+", q) if t]
    if not toks:
        return []
    variants = [{t, t.translate(_RU2EN), t.translate(_EN2RU).replace("ё", "е")} for t in toks]
    res = []
    for i in items:
        hay = (i["name"] + " " + (i.get("brand") or "") + " " + (i.get("code") or "")).lower().replace("ё", "е")
        flat = re.sub(r"[\s\-_.,/+()]", "", hay)               # «spf50» найдёт «SPF 50+», «watergel» — «Water-Gel»
        score = 0
        for vs in variants:
            best = 0
            for v in vs:
                if re.search(r"(^|[\s\-_/(.,+])" + re.escape(v), hay):
                    best = 3                                      # совпало с началом слова
                    break
                if v in hay or (len(v) > 2 and v in flat):
                    best = max(best, 1)
            if not best:
                break
            score += best
        else:
            code = (i.get("code") or "").lower()
            if q == code or q.lstrip("0") == code.lstrip("0"):
                score += 100
            if q in hay:
                score += 5
            res.append((-score, i.get("qty", 0) <= 0, i["name"].lower(), i))
    res.sort(key=lambda r: r[:3])
    return [r[3] for r in res[:limit]]


_api_uid = [None]
_DIFF_NAMES = {"description": "комментарий", "agent": "контрагент", "moment": "дата", "applicable": "проведение", "deliveryPlannedMoment": "план. дата отгрузки",
               "store": "склад", "organization": "организация", "project": "проект", "salesChannel": "канал продаж", "attributes": "доп. поля",
               "rate": "валюта", "contract": "договор", "vatEnabled": "НДС", "name": "номер", "owner": "владелец", "group": "отдел"}


def _nm(v):
    return (v or {}).get("name", "") if isinstance(v, dict) else str(v or "")


def _audit_text(ev):
    t = ev.get("eventType")
    if t == "create":
        return "Заказ создан"
    if t == "delete":
        return "Заказ удалён"
    if t == "print":
        return "Печать / выгрузка документа"
    if t != "update":
        return t or ""
    out = []
    for k, v in (ev.get("diff") or {}).items():
        if k == "state":
            out.append(f"Статус: «{_nm(v.get('oldValue'))}» → «{_nm(v.get('newValue'))}»")
        elif k == "sum":
            out.append(f"Сумма: {v.get('oldValue', 0):,.0f} → {v.get('newValue', 0):,.0f} ₸".replace(",", " "))
        elif k == "positions":
            for pv in v if isinstance(v, list) else []:
                a, b_ = pv.get("oldValue"), pv.get("newValue")
                nm = _nm((b_ or a or {}).get("assortment")) or "товар"
                if b_ and not a:
                    out.append(f"＋ {nm} — {b_.get('quantity', 0):g} шт. × {b_.get('price', 0):,.0f} ₸".replace(",", " "))
                elif a and not b_:
                    out.append(f"− {nm} ({a.get('quantity', 0):g} шт.)")
                elif a and b_:
                    if a.get("quantity") != b_.get("quantity"):
                        out.append(f"{nm}: {a.get('quantity', 0):g} → {b_.get('quantity', 0):g} шт.")
                    if a.get("price") != b_.get("price"):
                        out.append(f"{nm}: цена {a.get('price', 0):,.0f} → {b_.get('price', 0):,.0f} ₸".replace(",", " "))
        elif k in ("shipmentAddress", "shipmentAddressFull", "updated", "payedSum", "shippedSum", "invoicedSum", "reservedSum"):
            continue
        else:
            out.append("Изменено: " + _DIFF_NAMES.get(k, k))
    return "\n".join(out)


@bp.get("/admin/api/orders/<number>/history")
@need("orders")
def order_history(number):
    """История заказа: правки из панели (кто из команды) + журнал МойСклад (правки прямо в МойСклад и сценарии)."""
    items, warn = [], ""
    try:
        with inbox.db() as d:
            rows = d.run("SELECT at, who, text FROM order_log WHERE number=%s ORDER BY at DESC LIMIT 200", (str(number),), many=True)
        items += [{"at": r[0], "who": r[1], "text": r[2], "src": "panel"} for r in rows]
    except Exception:
        pass                                           # таблицы ещё нет — правок из панели не было
    panel_at = [i["at"] for i in items]
    oid = request.args.get("id", "")
    try:
        if not oh.re.fullmatch(r"[0-9a-f-]{36}", oid):
            rr = oh.ms("GET", "/entity/customerorder", params={"filter": f"name={number}", "limit": 1}, timeout=15)["rows"]
            oid = rr[0]["id"] if rr else ""
        if oid:
            if _api_uid[0] is None:
                try:
                    _api_uid[0] = oh.ms("GET", "/context/employee", timeout=10).get("uid", "")
                except Exception:
                    _api_uid[0] = ""
            evs = oh.ms("GET", f"/entity/customerorder/{oid}/audit", params={"limit": 100}, timeout=20).get("rows", [])
            for ev in evs:
                txt = _audit_text(ev)
                if not txt:
                    continue
                try:
                    at = datetime.strptime(ev["moment"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=3))).timestamp()  # время МойСклад — Москва
                except Exception:
                    at = 0
                uid = ev.get("uid", "")
                if uid and uid == _api_uid[0] and ev.get("eventType") == "update" and any(abs(at - p) < 120 for p in panel_at):
                    continue                           # та же правка, что уже записана панелью с именем сотрудника
                who_ = "Сценарий МойСклад" if uid.startswith("system@") else ("Сайт / панель" if uid and uid == _api_uid[0] else uid.split("@")[0])
                items.append({"at": at, "who": who_, "text": txt, "src": "ms"})
    except Exception as e:
        print("История заказа из МойСклад:", e, flush=True)
        warn = "Журнал МойСклад сейчас не ответил — показаны только правки из панели"
    items.sort(key=lambda i: -i["at"])
    return jsonify(ok=True, items=items[:200], warn=warn)


@bp.get("/admin/api/orders-search")
@need("orders")
def order_search():
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify(ok=True, items=[])
    shown, _ = _all_items()
    out = smart_find(shown, q, 30)
    return jsonify(ok=True, items=[{"type": "product", "id": i["id"], "name": i["name"], "qty": i["qty"], "price": oh.unit_price(i, 1, True),
                                    "img": f"{oh.SITE_URL}/{i['img']}" if i.get("img") else ""} for i in out])


@bp.put("/admin/api/orders/<number>")
@need("orders")
def order_edit(number):
    """Правка заказа: состав, цены, комментарий. Комиссия банка пересчитывается сама."""
    b = request.get_json(silent=True) or {}
    lines = b.get("lines") or []
    if not isinstance(lines, list) or not lines:
        return jsonify(ok=False, error="В заказе должен остаться хотя бы один товар"), 400
    try:
        o, pos = _order_fetch(number)
        if not o:
            return jsonify(ok=False, error="Заказ не найден"), 404
        positions, goods, loader = [], 0, 0
        prod = [p for p in pos if p["assortment"]["meta"]["type"] in RESERVABLE]
        keep_res = not prod or any((p.get("reserve") or 0) > 0 for p in prod)
        if len(lines) > 1000:
            return jsonify(ok=False, error="Слишком много позиций"), 400
        for l in lines:
            qty, price = int(float(l.get("qty") or 0)), float(l.get("price") or 0)
            if qty <= 0 or price < 0 or not oh.re.fullmatch(r"[0-9a-f-]{36}", str(l.get("id", ""))) or l.get("type") not in ("product", "bundle", "variant", "service"):
                return jsonify(ok=False, error="Проверьте количество и цены"), 400
            p = {"quantity": qty, "price": round(price * 100), "assortment": oh.meta(l["type"], l["id"])}
            if l["type"] in RESERVABLE and keep_res:            # резерв остаётся как был: сняли — правка его не ставит
                p["reserve"] = qty
            if l.get("pos") and oh.re.fullmatch(r"[0-9a-f-]{36}", str(l["pos"])):
                p["id"] = l["pos"]
            positions.append(p)
            goods += qty * price
        had_fee = False
        for p in pos:                                  # грузчик остаётся как был, комиссия банка пересчитывается
            a = p["assortment"]
            if a.get("code") == oh.LOADER_CODE:
                loader += p["price"] / 100 * p["quantity"]
                positions.append({"id": p["id"], "quantity": p["quantity"], "price": p["price"], "assortment": {"meta": a["meta"]}})
            elif a["meta"]["type"] == "service" and a.get("name") == oh.FEE_NAME:
                had_fee = True
                feep = {"id": p["id"], "quantity": 1, "assortment": {"meta": a["meta"]}}
                positions.append(feep)
        if had_fee:
            feep["price"] = int((goods + loader) * oh.FEE_RATE + 0.5) * 100
        body = {"positions": positions}
        if "description" in b:
            body["description"] = str(b.get("description") or "")[:2000]
        oh.ms("PUT", f"/entity/customerorder/{o['id']}", json=body, timeout=55)
    except Exception as e:
        return jsonify(ok=False, error="Не удалось сохранить: " + str(e)[:200]), 502
    try:                                               # что поменялось — в журнал
        old = {p["assortment"]["id"]: (p["assortment"].get("name", ""), int(p["quantity"]), p["price"] / 100) for p in pos
               if p["assortment"].get("code") != oh.LOADER_CODE and p["assortment"].get("name") != oh.FEE_NAME}
        new = {str(l["id"]): (str(l.get("name") or ""), int(float(l["qty"])), float(l["price"])) for l in lines}
        ch = []
        for i, (n, q, pr) in new.items():
            if i not in old:
                ch.append(f"＋ {n or 'товар'} — {q} шт. × {pr:,.0f} ₸".replace(",", " "))
            else:
                n0, q0, p0 = old[i]
                if q != q0:
                    ch.append(f"{n or n0}: {q0} → {q} шт.")
                if abs(pr - p0) > 0.009:
                    ch.append(f"{n or n0}: цена {p0:,.0f} → {pr:,.0f} ₸".replace(",", " "))
        ch += [f"− {n} ({q} шт.)" for i, (n, q, _) in old.items() if i not in new]
        if "description" in b and str(b.get("description") or "")[:2000] != (o.get("description") or ""):
            ch.append("Изменён комментарий")
        if ch:
            _olog(number, "\n".join(ch))
    except Exception as e:
        print("Журнал правки заказа:", e, flush=True)
    oh._cache.pop("adm_orders", None)
    oh._pdfs.pop(str(number), None)                 # старый PDF больше не верен — собираем новый в фоне
    oh.pdf_prepare([(number, o["id"])])
    return jsonify(ok=True)


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
# Оптовый сайт — баннеры в доп. поле организации МойСклад (как раньше); розничный — в нашей базе (setting banners_retail).
# Высота карусели (size) и отдельная картинка для телефона (imgM) — только у розницы; картинки можно загрузить из панели.
BANNER_SIZES = ("s", "m", "l")
BANNER_IMG_MAX = 3 * 1024 * 1024
BANNER_IMG_TYPES = ("image/jpeg", "image/png", "image/webp")
_bimg_ready = [False]


def _bimg_db():
    d = inbox.db()
    if not _bimg_ready[0]:
        blob = "BYTEA" if inbox.PG else "BLOB"
        d.run(f"CREATE TABLE IF NOT EXISTS banner_img (id TEXT PRIMARY KEY, mime TEXT, data {blob}, at DOUBLE PRECISION)")
        d.c.commit()
        _bimg_ready[0] = True
    return d


def _site():
    return "retail" if request.args.get("site") == "retail" else "opt"


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
            img_m = str(s.get("imgM", "")).strip()[:300]
            if oh.re.match(r"https?://", img_m):
                out["imgM"] = img_m
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
    size = d.get("size") if d.get("size") in BANNER_SIZES else "m"
    return {"autoplaySec": min(max(int(d.get("autoplaySec") or 6), 3), 20), "size": size, "slides": slides}


def banners_saved():
    org = oh.ms("GET", f"/entity/organization/{oh.organization()}")
    for a in org.get("attributes") or []:
        if a.get("name") == ATTR_BANNERS and a.get("value"):
            try:
                return json.loads(a["value"])
            except Exception:
                return None
    return None


def banners_retail():
    with inbox.db() as d:
        raw = inbox.get_setting(d, "banners_retail", "")
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


@bp.get("/banners")
def banners_public():
    """Баннеры для сайта (?site=retail — розничного); пусто — сайт показывает свои баннеры по умолчанию."""
    retail = _site() == "retail"
    try:
        d = oh.cached("banners_retail" if retail else "banners", 60, banners_retail if retail else banners_saved)
    except Exception:
        d = None
    if not d:
        return jsonify(error="none"), 404
    body = {"autoplaySec": d.get("autoplaySec", 6), "slides": [s for s in d["slides"] if not s.get("off")]}
    if retail:
        body["size"] = d.get("size", "m")
    resp = jsonify(**body)
    resp.headers["Cache-Control"] = "public, max-age=60"
    return oh.cors(resp)


@bp.get("/banners/img/<bid>")
def banner_img(bid):
    if not re.fullmatch(r"[a-f0-9]{24}", bid):
        return jsonify(error="none"), 404
    with _bimg_db() as d:
        r = d.run("SELECT data, mime FROM banner_img WHERE id=%s", (bid,), one=True)
    if not r:
        return jsonify(error="none"), 404
    resp = oh.cors(oh.Response(bytes(r[0]), mimetype=r[1]))
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@bp.post("/admin/api/banners/upload")
@guard
def banner_upload():
    """Картинка баннера из панели: храним у себя, отдаём по постоянной ссылке /banners/img/<id>."""
    f = request.files.get("file")
    if not f:
        return jsonify(ok=False, error="Файл не выбран"), 400
    mime = (f.mimetype or "").lower()
    if mime not in BANNER_IMG_TYPES:
        return jsonify(ok=False, error="Нужна картинка JPG, PNG или WEBP"), 400
    data = f.read()
    if len(data) > BANNER_IMG_MAX:
        return jsonify(ok=False, error="Картинка больше 3 МБ — уменьшите её"), 413
    bid = secrets.token_hex(12)
    with _bimg_db() as d:
        d.run("INSERT INTO banner_img (id, mime, data, at) VALUES (%s,%s,%s,%s)", (bid, mime, data, time.time()))
    root = request.url_root.replace("http://", "https://", 1).rstrip("/")
    return jsonify(ok=True, url=f"{root}/banners/img/{bid}")


@bp.route("/admin/api/banners", methods=["GET", "PUT"])
@guard
def banners_admin():
    retail = _site() == "retail"
    if request.method == "PUT":
        d = _clean_banners(request.get_json(silent=True) or {})
        raw = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
        if retail:
            with inbox.db() as db_:
                inbox.set_setting(db_, "banners_retail", raw)
            oh._cache.pop("banners_retail", None)
            return jsonify(ok=True, **d)
        if len(raw) > 4000:
            return jsonify(ok=False, error="Слишком много текста — сократите баннеры"), 400
        oh.ms("PUT", f"/entity/organization/{oh.organization()}", json={"attributes": [{"meta": _attr("organization", ATTR_BANNERS, "text")["meta"], "value": raw}]})
        oh._cache.pop("banners", None)
        return jsonify(ok=True, **d)
    if retail:
        d = banners_retail()
        return jsonify(ok=True, saved=bool(d), **_clean_banners(d or {"slides": RETAIL_BANNERS_DEFAULT}))
    d = banners_saved()
    if not d:                                  # ещё не сохраняли — берём баннеры, которые сейчас на сайте
        try:
            d = oh.requests.get(f"{oh.SITE_URL}/banners.json", timeout=10).json()
        except Exception:
            d = {"autoplaySec": 6, "slides": []}
    return jsonify(ok=True, saved=bool(banners_saved is not None and d), **_clean_banners(d))


# такие же баннеры по умолчанию у розничного сайта (retail/banners.js) — показываются, пока в панели ничего не сохранено
RETAIL_BANNERS_DEFAULT = [
    {"title": "Корейская косметика с доставкой", "text": "Оригинальный уход со склада в Алматы. Доставка по городу и по всему Казахстану.",
     "tags": ["Оригинал", "Доставка по Казахстану"], "bg": "#14503C"},
    {"title": "Новинки недели", "text": "Свежие поступления — смотрите первыми.", "button": "Смотреть новинки",
     "link": {"sort": "new"}, "bg": "#1E3A5F"},
]


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
        rows = d.run("SELECT id, name, username, status, unread, last_at, last_text, chat_id FROM conv WHERE last_at IS NULL OR last_at>0 ORDER BY last_at DESC LIMIT 100", many=True)   # пустые диалоги (клиенты, добавленные в группу) — не показываем
    return jsonify(ok=True, convs=[{"id": r[0], "name": r[1], "username": r[2], "status": r[3], "unread": r[4], "at": r[5], "text": r[6],
                                    "channel": inbox.channel(r[7]), "phone": wa.number(r[7]) if wa.is_wa(r[7]) else ""} for r in rows])


def _msg_json(m):
    media = None
    if len(m) > 5 and m[5]:
        try:
            media = json.loads(m[5])
            media["url"] = f"/admin/api/inbox/file/{media['id']}?t={_sig('fl' + media['id'])}"
        except Exception:
            media = None
    return {"id": m[0], "role": m[1], "text": m[2], "at": m[4], "media": media, "edited": (m[6] if len(m) > 6 else None) or 0,
            "photo": (f"/admin/api/inbox/photo/{m[3]}?t={_sig('ph' + m[3])}" if m[3] else "")}


@bp.get("/admin/api/inbox/conv/<int:cid>")
@need("inbox")
def inbox_conv(cid):
    with inbox.db() as d:
        c = d.run("SELECT id, name, username, status, chat_id FROM conv WHERE id=%s", (cid,), one=True)
        if not c:
            return jsonify(ok=False, error="Диалог не найден"), 404
        d.run("UPDATE conv SET unread=0 WHERE id=%s", (cid,))
        ms_ = d.run("SELECT id, role, text, photo, at, media, edited FROM msg WHERE conv_id=%s ORDER BY id DESC LIMIT 100", (cid,), many=True)[::-1]
    lastc = max([m[4] for m in ms_ if m[1] == "client"] or [0])
    isw, ch = wa.is_wa(c[4]), inbox.channel(c[4])
    age = time.time() - lastc
    opn = age < 86400 - 60 if isw else age < 7 * 86400 - 60 if ch == "ig" else True      # WhatsApp — 24 ч, Instagram — 7 дней (после 24 ч с меткой «ответ менеджера»)
    return jsonify(ok=True, conv={"id": c[0], "name": c[1], "username": c[2], "status": c[3], "channel": ch,
                                  "phone": wa.number(c[4]) if isw else "", "open": opn, "late": ch == "ig" and age >= 86400 - 60},
                   messages=[_msg_json(m) for m in ms_])


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
        try:
            ext = inbox.send_text(c[0], text)
        except Exception as e:
            return jsonify(ok=False, error=str(e)[:300]), 502
        inbox.save_msg(d, cid, "manager", text, ext=ext)
        d.run("UPDATE conv SET status='manager' WHERE id=%s", (cid,))     # менеджер ответил — ИИ молчит
    return jsonify(ok=True)


@bp.post("/admin/api/inbox/conv/<int:cid>/msg/<int:mid>/edit")
@need("inbox")
def inbox_edit(cid, mid):
    """Изменить отправленный текст (менеджера или ИИ). Telegram и чат сайта — правка на месте, WhatsApp и Instagram — «Исправление» отдельным сообщением."""
    text = str((request.get_json(silent=True) or {}).get("text", "")).strip()[:3500]
    if not text:
        return jsonify(ok=False, error="Пустое сообщение"), 400
    with inbox.db() as d:
        c = d.run("SELECT chat_id FROM conv WHERE id=%s", (cid,), one=True)
        m = d.run("SELECT role, text, photo, media, ext FROM msg WHERE id=%s AND conv_id=%s", (mid, cid), one=True)
        if not c or not m:
            return jsonify(ok=False, error="Сообщение не найдено"), 404
        if m[0] not in ("manager", "ai") or m[2] or m[3]:
            return jsonify(ok=False, error="Изменить можно только свой текст без вложений"), 400
        if text == (m[1] or ""):
            return jsonify(ok=True, inplace=True)
        try:
            inplace = inbox.edit_text(c[0], m[4], text)
        except Exception as e:
            return jsonify(ok=False, error=str(e)[:300]), 502
        d.run("UPDATE msg SET text=%s, edited=%s WHERE id=%s", (text, time.time(), mid))
        last = d.run("SELECT id FROM msg WHERE conv_id=%s ORDER BY id DESC LIMIT 1", (cid,), one=True)
        if last and last[0] == mid:
            d.run("UPDATE conv SET last_text=%s WHERE id=%s", (text[:120], cid))
    return jsonify(ok=True, inplace=inplace)


@bp.post("/admin/api/inbox/conv/<int:cid>/status")
@need("inbox")
def inbox_set_status(cid):
    st = (request.get_json(silent=True) or {}).get("status")
    if st not in ("ai", "manager", "closed"):
        return jsonify(ok=False, error="Неверный статус"), 400
    with inbox.db() as d:
        d.run("UPDATE conv SET status=%s WHERE id=%s", (st, cid))
    return jsonify(ok=True)


MAX_UPLOAD = 20 * 1024 * 1024        # лимит Telegram на скачивание файлов ботом


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        import shutil
        return shutil.which("ffmpeg")


def _convert(data, args, suffix_in=".bin"):
    """Прогоняет аудио через ffmpeg. Возвращает байты или None, если ffmpeg недоступен или упал."""
    import subprocess
    import tempfile
    exe = _ffmpeg()
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in" + suffix_in)
        out = os.path.join(td, "out" + args[-1])
        open(src, "wb").write(data)
        r = subprocess.run([exe, "-y", "-loglevel", "error", "-i", src] + args[:-1] + [out], capture_output=True, timeout=60)
        return open(out, "rb").read() if r.returncode == 0 and os.path.exists(out) else None


def _send_file_wa(chat, data, name, mime, kind, caption, dur):
    """Файл менеджера -> WhatsApp. Возвращает (media, photo) для записи в диалог."""
    if kind == "voice":
        ogg = _convert(data, ["-vn", "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k", "-application", "voip", ".ogg"],      # WhatsApp принимает голосовые только в моно
                       ".m4a" if ("mp4" in mime or "m4a" in mime) else ".webm")
        if not ogg:
            raise RuntimeError("Не удалось подготовить голосовое для WhatsApp")
        return {"t": "voice", "id": "wa:" + wa.send_media(chat, "audio", ogg, "voice.ogg", "audio/ogg"), "dur": int(float(dur or 0))}, None
    if mime in ("image/jpeg", "image/png") and len(data) <= 5 * 1024 * 1024:
        return None, "wa:" + wa.send_media(chat, "image", data, name, mime, caption)
    if mime in ("video/mp4", "video/3gpp") and len(data) <= 16 * 1024 * 1024:
        return {"t": "video", "id": "wa:" + wa.send_media(chat, "video", data, name, mime, caption), "dur": 0}, None
    return {"t": "doc", "id": "wa:" + wa.send_media(chat, "document", data, name, mime, caption), "name": name, "size": len(data), "mime": mime}, None


def _send_file_ig(chat, data, name, mime, kind, caption, dur):
    """Файл менеджера -> Instagram (голосовое — аудиовложением m4a). Возвращает (media, photo) для записи в диалог."""
    if kind == "voice":
        m4a = _convert(data, ["-vn", "-ac", "1", "-c:a", "aac", "-b:a", "64k", ".m4a"],
                       ".m4a" if ("mp4" in mime or "m4a" in mime) else ".ogg" if "ogg" in mime else ".webm")
        if not m4a and ("mp4" in mime or "m4a" in mime or "aac" in mime):
            m4a = data
        if not m4a:
            raise RuntimeError("Не удалось подготовить голосовое для Instagram")
        return {"t": "voice", "id": ig.send_media(chat, "audio", m4a, "voice.m4a", "audio/mp4"), "dur": int(float(dur or 0))}, None
    if mime in ("image/jpeg", "image/png", "image/gif") and len(data) <= 8 * 1024 * 1024:
        return None, ig.send_media(chat, "image", data, name, mime, caption)
    if mime.startswith("video/") and len(data) <= 25 * 1024 * 1024:
        return {"t": "video", "id": ig.send_media(chat, "video", data, name, mime, caption), "dur": 0}, None
    if mime.startswith("audio/"):
        return {"t": "voice", "id": ig.send_media(chat, "audio", data, name, mime, caption), "dur": 0}, None
    return {"t": "doc", "id": ig.send_media(chat, "file", data, name, mime, caption), "name": name, "size": len(data), "mime": mime}, None


@bp.post("/admin/api/inbox/conv/<int:cid>/send-file")
@need("inbox")
def inbox_send_file(cid):
    """Менеджер отправляет клиенту фото, файл, голосовое (kind=voice) или видеокружок (kind=round) из панели."""
    f = request.files.get("file")
    if not f:
        return jsonify(ok=False, error="Файл не выбран"), 400
    data = f.read()
    if len(data) > MAX_UPLOAD:
        return jsonify(ok=False, error="Файл больше 20 МБ"), 413
    caption = str(request.form.get("caption", "")).strip()[:900]
    kind, mime = request.form.get("kind", ""), (f.mimetype or "application/octet-stream")
    name = (f.filename or "file").replace("/", "_")[:80]
    with inbox.db() as d:
        c = d.run("SELECT chat_id FROM conv WHERE id=%s", (cid,), one=True)
        if not c:
            return jsonify(ok=False, error="Диалог не найден"), 404
        try:
            deliver_file(d, cid, c[0], data, name, mime, kind, caption, request.form.get("dur", 0))
        except Exception as e:
            return jsonify(ok=False, error=str(e)[:300]), 502
        d.run("UPDATE conv SET status='manager' WHERE id=%s", (cid,))
    return jsonify(ok=True)


def deliver_file(d, cid, chat, data, name, mime, kind="", caption="", dur=0):
    """Отправить файл клиенту в его мессенджер и записать в диалог (из чата панели и из рассылок). Ошибка отправки — исключение."""
    if kind == "round":                                 # видеокружок: квадрат 384×384, mp4 — как «кружок» Telegram
        mp4 = _convert(data, ["-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=384:384", "-c:v", "libx264", "-preset", "veryfast",
                              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", ".mp4"],
                       ".mp4" if "mp4" in mime else ".webm")
        if mp4:
            data, mime, name = mp4, "video/mp4", "round.mp4"
    media, photo = None, None
    text = caption if kind != "voice" else ""
    if str(chat).startswith("web:"):                    # чат сайта: файл храним у себя, сайт заберёт опросом
        import webchat
        fid = webchat.store(chat, data, mime, name)
        if mime.startswith("image/") and kind != "voice":
            photo = fid
        else:
            media = {"t": "voice" if kind == "voice" else "video" if kind == "round" else "doc", "id": fid, "name": name, "size": len(data), "mime": mime}
            if kind == "round":
                media["round"] = 1
        inbox.save_msg(d, cid, "manager", text, photo, media=media)
        return
    if wa.is_wa(chat) or ig.is_ig(chat):
        media, photo = (_send_file_wa if wa.is_wa(chat) else _send_file_ig)(chat, data, name, mime, kind, caption, dur)
        if kind == "round" and media:                   # кружков там нет — уходит обычным видео, в панели показываем кружком
            media["round"] = 1
        inbox.save_msg(d, cid, "manager", text, photo, media=media)
        return
    if kind == "voice":
        dur = int(float(dur or 0))
        if "mp4" in mime or "m4a" in mime or "aac" in mime:
            voice, vname, vmime = data, "voice.m4a", "audio/mp4"
        else:                                           # webm/ogg с Opus -> ogg для голосового Telegram
            ogg = _convert(data, ["-vn", "-c:a", "libopus", "-b:a", "32k", ".ogg"], ".webm")
            voice, vname, vmime = ogg or data, "voice.ogg" if ogg else name, "audio/ogg" if ogg else mime
        try:
            j = oh.tg("sendVoice", chat_id=chat, duration=dur, _files={"voice": (vname, voice, vmime)})
            media = {"t": "voice", "id": j["result"]["voice"]["file_id"], "dur": dur}
        except Exception:                               # не приняли как голосовое — отправим как файл
            j = oh.tg("sendDocument", chat_id=chat, _files={"document": (vname, voice, vmime)})
            media = {"t": "doc", "id": j["result"]["document"]["file_id"], "name": vname, "size": len(voice)}
    elif kind == "round" and mime == "video/mp4":
        dur = int(float(dur or 0))
        try:
            j = oh.tg("sendVideoNote", chat_id=chat, duration=dur, length=384, _files={"video_note": ("round.mp4", data, mime)})
            media = {"t": "video", "id": j["result"]["video_note"]["file_id"], "dur": dur, "round": 1}
        except Exception:                               # не приняли как кружок — обычное видео
            j = oh.tg("sendVideo", chat_id=chat, _files={"video": ("round.mp4", data, mime)})
            media = {"t": "video", "id": j["result"]["video"]["file_id"], "dur": dur, "round": 1}
    elif mime.startswith("image/") and len(data) <= 10 * 1024 * 1024 and mime != "image/gif":
        j = oh.tg("sendPhoto", chat_id=chat, caption=caption, _files={"photo": (name, data, mime)})
        photo = j["result"]["photo"][-1]["file_id"]
    elif mime.startswith("video/") and len(data) <= 20 * 1024 * 1024:
        j = oh.tg("sendVideo", chat_id=chat, caption=caption, _files={"video": (name, data, mime)})
        media = {"t": "video", "id": j["result"]["video"]["file_id"], "dur": j["result"]["video"].get("duration", 0)}
    else:
        j = oh.tg("sendDocument", chat_id=chat, caption=caption, _files={"document": (name, data, mime)})
        media = {"t": "doc", "id": j["result"]["document"]["file_id"], "name": name, "size": len(data), "mime": mime}
    inbox.save_msg(d, cid, "manager", text, photo, media=media)


@bp.post("/admin/api/inbox/conv/<int:cid>/send-location")
@need("inbox")
def inbox_send_location(cid):
    """Геопозиция менеджера: Telegram и WhatsApp — точкой на карте, Instagram и сайт — ссылкой на карту."""
    b = request.get_json(silent=True) or {}
    try:
        lat, lon = float(b.get("lat")), float(b.get("lon"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Нет координат"), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify(ok=False, error="Неверные координаты"), 400
    link = f"https://maps.google.com/?q={lat:.6f},{lon:.6f}"
    text = "📍 Геопозиция\n" + link
    with inbox.db() as d:
        c = d.run("SELECT chat_id FROM conv WHERE id=%s", (cid,), one=True)
        if not c:
            return jsonify(ok=False, error="Диалог не найден"), 404
        chat = c[0]
        try:
            if wa.is_wa(chat):
                wa._call("POST", f"{wa.cfg()['phone_id']}/messages", json={"messaging_product": "whatsapp", "to": wa.number(chat),
                                                                         "type": "location", "location": {"latitude": lat, "longitude": lon}})
            elif inbox.channel(chat) == "tg":
                oh.tg("sendLocation", chat_id=chat, latitude=lat, longitude=lon)
            else:
                inbox.send_text(chat, text)
        except Exception as e:
            return jsonify(ok=False, error=str(e)[:300]), 502
        inbox.save_msg(d, cid, "manager", text)
        d.run("UPDATE conv SET status='manager' WHERE id=%s", (cid,))
    return jsonify(ok=True)


@bp.get("/admin/api/inbox/file/<path:file_id>")
def inbox_file(file_id):
    """Файл из Telegram для показа в панели (голосовые, видео, документы). ?fmt=mp3 — перекодировать для браузеров без Opus (iPhone)."""
    if not oh.ORDER_SECRET or not oh.hmac.compare_digest(request.args.get("t", ""), _sig("fl" + file_id)):
        return "", 403
    if file_id.startswith(("wa:", "ig:", "web:")):
        try:
            data, mime = _media_mod(file_id).download(file_id)
        except Exception as e:
            return str(e), 502
        fname = "file" + (mimetypes.guess_extension(mime.split(";")[0]) or "")
    else:
        info = oh.tg("getFile", file_id=file_id)["result"]
        r = oh.requests.get(f"https://api.telegram.org/file/bot{oh.BOT}/{info['file_path']}", timeout=60)
        data, fname = r.content, info["file_path"].rsplit("/", 1)[-1]
        mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        if fname.endswith((".oga", ".ogg")):
            mime = "audio/ogg"
    if request.args.get("fmt") == "mp3":
        out = _convert(data, ["-vn", "-c:a", "libmp3lame", "-b:a", "48k", ".mp3"], os.path.splitext(fname)[1] or ".ogg")
        if out:
            data, mime = out, "audio/mpeg"
    import io
    from flask import send_file
    return send_file(io.BytesIO(data), mimetype=mime, conditional=True, max_age=86400,       # conditional — поддержка Range, без неё Safari не играет аудио
                     as_attachment=bool(request.args.get("dl")), download_name=request.args.get("name") or fname)


def _media_mod(file_id):
    if file_id.startswith("web:"):
        import webchat
        return webchat
    return ig if file_id.startswith("ig:") else wa


@bp.get("/admin/api/inbox/photo/<path:file_id>")
def inbox_photo(file_id):
    t = request.args.get("t", "")
    if not oh.ORDER_SECRET or not oh.hmac.compare_digest(t, _sig("ph" + file_id)):
        return "", 403
    if file_id.startswith(("wa:", "ig:", "web:")):
        try:
            data, mime = _media_mod(file_id).download(file_id)
        except Exception as e:
            return str(e), 502
        return oh.Response(data, mimetype=mime if mime.startswith("image/") else "image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
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
            for k, on in (j.get("channels") or {}).items():          # ИИ по каналам: wa / ig / tg / web
                if k in inbox.AI_CHANNELS:
                    inbox.set_setting(d, "ai_ch_" + k, "1" if on else "0")
            if "rules" in j:
                inbox.set_setting(d, "rules", str(j["rules"]).strip()[:6000] or inbox.DEFAULT_RULES)
            return jsonify(ok=True)
        kb = d.run("SELECT id, title, body FROM kb ORDER BY id", many=True)
        return jsonify(ok=True, enabled=inbox.get_setting(d, "ai_enabled", "1") == "1", key=bool(inbox.OPENAI_KEY), model=inbox.OPENAI_MODEL,
                       channels=inbox.ai_channels(d),
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


# ---------- каналы: подключение WhatsApp без правки сервера ----------
def _mask(v):
    return ("•" * 8 + v[-4:]) if len(v) > 6 else ""


@bp.route("/admin/api/channels", methods=["GET", "PUT"])
@guard
def channels():
    if request.method == "PUT":
        b = request.get_json(silent=True) or {}
        with inbox.db() as d:
            for k in ("phone_id", "token", "secret", "app_id", "config_id", "config_coex"):
                v = str(b.get(k, "")).strip()
                if v and "•" not in v:                  # маска означает «не менять»
                    inbox.set_setting(d, "wa_" + k, v)
                elif k in b and not v:
                    inbox.set_setting(d, "wa_" + k, "")
        wa.reset_cache()                                # читаем только после записи (коммит при выходе из with)
    c = wa.cfg()
    base = (oh.PUBLIC_URL or request.url_root).rstrip("/")
    return jsonify(ok=True, tg={"ok": bool(oh.BOT and oh.OWNER)}, persist=bool(inbox.PG),
                   wa={"ok": wa.configured(), "phone_id": c["phone_id"], "token": _mask(c["token"]), "secret": _mask(c["secret"]),
                       "app_id": c["app_id"], "config_id": c["config_id"], "config_coex": c["config_coex"],
                       "ready": bool(c["app_id"] and c["config_id"] and c["secret"]), "readyCoex": bool(c["app_id"] and (c["config_coex"] or c["config_id"]) and c["secret"]),
                       "url": f"{base}/wa/{oh.HOOK_SECRET}", "verify": oh.HOOK_SECRET},
                   ig=_ig_info(base))


def _ig_info(base):
    c = ig.cfg()
    own = bool(c["secret"]) and c["secret"] != wa.cfg().get("secret")          # пусто — используется App Secret от WhatsApp
    return {"ok": ig.configured(), "page_id": c["page_id"], "token": _mask(c["token"]), "secret": _mask(c["secret"]) if own else "",
            "shared_secret": bool(c["secret"]) and not own, "page": c.get("page_name", ""), "username": c.get("username", ""),
            "url": f"{base}/ig/{oh.HOOK_SECRET}", "verify": oh.HOOK_SECRET}


@bp.post("/admin/api/channels/ig")
@guard
def ig_connect():
    """Instagram: «Сохранить и проверить» — проверка токена страницы, подписка на сообщения, сохранение настроек."""
    b = request.get_json(silent=True) or {}
    base = (oh.PUBLIC_URL or request.url_root).rstrip("/")
    try:
        j = ig.connect(str(b.get("page_id", "")), str(b.get("token", "")), str(b.get("secret", "")), f"{base}/ig/{oh.HOOK_SECRET}", oh.HOOK_SECRET, base)
    except oh.requests.exceptions.RequestException:
        return jsonify(ok=False, error="Не удалось связаться с Meta. Повторите через минуту."), 502
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:800]), 400
    return jsonify(ok=True, **j)


@bp.post("/admin/api/channels/wa-test")
@guard
def wa_test():
    try:
        j = wa.check()
        return jsonify(ok=True, name=j.get("verified_name", ""), phone=j.get("display_phone_number", ""), quality=j.get("quality_rating", ""))
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 400


@bp.post("/admin/api/channels/wa-connect")
@guard
def wa_connect():
    """Финал кнопки «Подключить WhatsApp»: страница прислала code из окна Facebook и id номера."""
    b = request.get_json(silent=True) or {}
    code, pid, wid = str(b.get("code", "")), str(b.get("phone_id", "") or ""), str(b.get("waba_id", ""))
    coex = bool(b.get("coex"))
    if not code:
        return jsonify(ok=False, error="Facebook не передал данные номера. Попробуйте ещё раз и дойдите до конца."), 400
    base = (oh.PUBLIC_URL or request.url_root).rstrip("/")
    try:
        page = str(b.get("redirect", "")).split("#")[0][:300]
        origin = request.url_root.rstrip("/")
        reds = []
        for ru in ("", None, page, origin, origin + "/", origin + "/admin", "https://www.facebook.com/connect/login_success.html"):      # None — параметр не передавать
            if ru not in reds and (ru is None or ru == "" or ru.startswith("http")):
                reds.append(ru)
        j = wa.connect(code, pid, wid, f"{base}/wa/{oh.HOOK_SECRET}", oh.HOOK_SECRET, coex, reds)
    except oh.requests.exceptions.RequestException:
        return jsonify(ok=False, error="Не удалось связаться с Meta. Повторите через минуту."), 502
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:1200]), 400
    return jsonify(ok=True, name=j.get("verified_name", ""), phone=j.get("display_phone_number", ""))


@bp.post("/admin/api/channels/wa-token")
@guard
def wa_token():
    """Запасное подключение: токен системного пользователя (если окно Facebook не отдало код)."""
    b = request.get_json(silent=True) or {}
    tok_, pid, wid = str(b.get("token", "")).strip(), str(b.get("phone_id", "")).strip(), str(b.get("waba_id", "")).strip()
    if not tok_ or "•" in tok_:
        return jsonify(ok=False, error="Вставьте токен целиком"), 400
    base = (oh.PUBLIC_URL or request.url_root).rstrip("/")
    try:
        j = wa.connect_token(tok_, pid, f"{base}/wa/{oh.HOOK_SECRET}", oh.HOOK_SECRET, wid)
    except oh.requests.exceptions.RequestException:
        return jsonify(ok=False, error="Не удалось связаться с Meta. Повторите через минуту."), 502
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:1200]), 400
    return jsonify(ok=True, name=j.get("verified_name", ""), phone=j.get("display_phone_number", ""))


# ---------- зеркало МойСклад (этап 1 миграции, только чтение; раздел панели #mirror — только владелец) ----------
import mirror  # noqa: E402


@bp.get("/admin/api/mirror")
@guard
def mirror_status():
    return jsonify(ok=True, **mirror.status())


@bp.post("/admin/api/mirror/flag")
@guard
def mirror_flag():
    on = bool((request.get_json(silent=True) or {}).get("on"))
    mirror.set_enabled(on)
    if on:
        mirror.tick_bg()
    return jsonify(ok=True, **mirror.status())


@bp.post("/admin/api/mirror/sync")
@guard
def mirror_sync():
    return jsonify(ok=True, started=mirror.tick_bg(), **mirror.status())


@bp.post("/admin/api/mirror/recon")
@guard
def mirror_recon():
    return jsonify(ok=True, started=mirror.recon_bg(), **mirror.status())


@bp.get("/admin/api/mirror/stock")
@guard
def mirror_stock():
    return jsonify(ok=True, items=mirror.stock_of(request.args.get("q", "")))


# ---------- модуль «Доставка»: подключение Яндекс / СДЭК, склад, цена для клиента (только владелец) ----------
import delivery  # noqa: E402


@bp.route("/admin/api/delivery", methods=["GET", "POST"])
@guard
def delivery_conf():
    if request.method == "POST":
        delivery.save(request.get_json(silent=True) or {})
    return jsonify(ok=True, **delivery.public_conf())


@bp.route("/admin/api/delivery/estimate", methods=["GET", "POST"])
@guard
def delivery_estimate_route():
    import delivery_estimate
    if request.method == "POST":
        delivery_estimate.run_bg()
    return jsonify(ok=True, **delivery_estimate.last())


@bp.post("/admin/api/delivery/test")
@guard
def delivery_test():
    svc = str((request.get_json(silent=True) or {}).get("svc", ""))
    try:
        msg = delivery.test(svc)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 400
    return jsonify(ok=True, message=msg)


# ---------- модуль «Финансы» → «Счета»: куда поступают деньги (только владелец) ----------
import finance  # noqa: E402


def _fin_who():
    return (who() or {}).get("name", "") or "Владелец"


@bp.get("/admin/api/accounts")
@guard
def fin_state():
    return jsonify(ok=True, **finance.state())


@bp.post("/admin/api/accounts/save")
@guard
def fin_save():
    try:
        finance.save_account(request.get_json(silent=True) or {}, _fin_who())
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, **finance.state())


@bp.post("/admin/api/accounts/<int:aid>/delete")
@guard
def fin_delete(aid):
    try:
        finance.delete_account(aid, _fin_who())
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, **finance.state())


@bp.post("/admin/api/accounts/routes")
@guard
def fin_routes():
    finance.save_routes(request.get_json(silent=True) or {}, _fin_who())
    return jsonify(ok=True, **finance.state())


# ---------- отзывы о товарах (Товары → Отзывы) ----------
@bp.get("/admin/api/reviews")
@need("products")
def reviews_list():
    import reviews
    st = request.args.get("status", "new")
    st = st if st in ("new", "ok", "hidden") else "new"
    with reviews.db() as d:
        rows = d.run(f"SELECT {reviews.COLS} FROM review WHERE status=%s ORDER BY at DESC LIMIT 200", (st,), many=True)
        cnt = dict(d.run("SELECT status, COUNT(*) FROM review GROUP BY status", many=True))
    return jsonify(ok=True, items=[reviews._row(r, admin=True) for r in rows], counts={k: int(cnt.get(k, 0)) for k in ("new", "ok", "hidden")},
                   **reviews.conf())


@bp.post("/admin/api/reviews/settings")
@need("products")
def reviews_settings():
    import reviews
    j = request.get_json(silent=True) or {}
    with reviews.db() as d:
        for k in ("on", "premod"):
            if k in j:
                inbox.set_setting(d, "reviews_" + k, "1" if j[k] else "0")
    return jsonify(ok=True, **reviews.conf())


@bp.post("/admin/api/reviews/<int:rid>")
@need("products")
def reviews_update(rid):
    import reviews
    j = request.get_json(silent=True) or {}
    with reviews.db() as d:
        if j.get("status") in ("ok", "hidden", "new"):
            d.run("UPDATE review SET status=%s WHERE id=%s", (j["status"], rid))
        if "answer" in j:
            d.run("UPDATE review SET answer=%s WHERE id=%s", (str(j["answer"]).strip()[:1500], rid))
        if j.get("delete"):
            d.run("DELETE FROM review WHERE id=%s", (rid,))
    oh._cache.pop("rv_sum", None)
    return jsonify(ok=True)


# ---------- подключаемые модули (по умолчанию выключены; см. modules.py) ----------
import modules  # noqa: E402


@bp.get("/admin/api/modules/on")
def modules_on():
    if not who():
        return jsonify(ok=False, error="Войдите заново"), 401
    return jsonify(ok=True, on=modules.on_map())


@bp.route("/admin/api/modules", methods=["GET", "PUT"])
@guard
def modules_admin():
    if request.method == "PUT":
        b = request.get_json(silent=True) or {}
        try:
            modules.update(str(b.get("id", "")), on=(bool(b["on"]) if "on" in b else None), src=b.get("source"))
        except KeyError:
            return jsonify(ok=False, error="Нет такого модуля"), 404
    return jsonify(ok=True, modules=modules.listing())


@bp.get("/admin/api/finance")
@guard
def finance_money():
    if not modules.enabled("finance"):
        return jsonify(ok=False, error="Модуль «Финансы» выключен"), 404
    return jsonify(ok=True, source=modules.source("finance"), **mirror.finance_view())



@bp.get("/admin/api/warehouse")
@guard
def warehouse_stock():
    if not modules.enabled("warehouse"):
        return jsonify(ok=False, error="Модуль «Склад» выключен"), 404
    a = request.args
    try:
        off = max(0, int(a.get("offset") or 0))
    except ValueError:
        off = 0
    return jsonify(ok=True, source=modules.source("warehouse"),
                   **mirror.warehouse_view(a.get("q", "")[:100], a.get("store", "")[:64], a.get("mode", "all"), 50, off))


# ---------- Склад: приёмка и списание с двойной записью в МойСклад (флаг feat_wh_ops, см. whops.py) ----------
import whops  # noqa: E402


@bp.route("/admin/api/warehouse/ops", methods=["GET", "POST"])
@guard
def warehouse_ops():
    if not modules.enabled("warehouse"):
        return jsonify(ok=False, error="Модуль «Склад» выключен"), 404
    if request.method == "POST":
        if not whops.enabled():
            return jsonify(ok=False, error="Приёмка и списание из панели выключены"), 403
        b = request.get_json(silent=True) or {}
        try:
            op = whops.create(str(b.get("kind", "")), str(b.get("store", "")), b.get("lines") or [],
                              str(b.get("agent", "")), str(b.get("descr", "")), _fin_who())
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        return jsonify(ok=True, op=op)
    return jsonify(ok=True, enabled=whops.enabled(), ops=whops.recent())


@bp.post("/admin/api/warehouse/ops/flag")
@guard
def warehouse_ops_flag():
    if not modules.enabled("warehouse"):
        return jsonify(ok=False, error="Модуль «Склад» выключен"), 404
    whops.set_enabled(bool((request.get_json(silent=True) or {}).get("on")))
    return jsonify(ok=True, enabled=whops.enabled())


@bp.post("/admin/api/warehouse/ops/<op_id>/retry")
@guard
def warehouse_ops_retry(op_id):
    if not whops.enabled():
        return jsonify(ok=False, error="Приёмка и списание из панели выключены"), 403
    op = whops.retry(op_id)
    if not op:
        return jsonify(ok=False, error="Операция не найдена"), 404
    return jsonify(ok=True, op=op)


@bp.get("/admin/api/warehouse/agents")
@guard
def warehouse_agents():
    if not whops.enabled():
        return jsonify(ok=False, error="Приёмка и списание из панели выключены"), 403
    return jsonify(ok=True, agents=whops.agents(request.args.get("q", "")[:100]))
