"""Instagram Direct (официальный Instagram Messaging API от Meta, через страницу Facebook): приём и отправка сообщений для инбокса.
Настройки (Page ID, Page access token, App Secret) вводятся в панели «Ассистент → Каналы» и хранятся в базе (ключи ig_*); можно и через env IG_PAGE_ID / IG_TOKEN / IG_APP_SECRET.
Чат в инбоксе имеет chat_id вида «ig:<IGSID>». Вложения Instagram отдаёт временными ссылками, поэтому файлы сохраняем в базе (таблица ig_media, id «ig:<ключ>»);
наши файлы для отправки клиенту тоже кладём туда и отдаём Meta по подписанной ссылке /ig/f/<ключ>."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

import requests
from flask import Blueprint, Response, request

import order_hook as oh

bp = Blueprint("ig", __name__)
GRAPH_VER = "v21.0"
GRAPH = "https://graph.facebook.com/" + GRAPH_VER
DAY = 86400
_seen = {}                                   # mid входящих сообщений: Meta повторяет доставку, дубли не нужны
_cfg = {"t": 0.0, "v": {}}
_names = {}                                  # IGSID -> (время, имя, юзернейм)
_media_ready = [False]


def cfg():
    """Настройки Instagram (кэш 30 с): база, затем env. App Secret можно не вводить — берём от WhatsApp (одно приложение Meta)."""
    if time.time() - _cfg["t"] < 30:
        return _cfg["v"]
    v = {"page_id": os.environ.get("IG_PAGE_ID", ""), "token": os.environ.get("IG_TOKEN", ""), "secret": os.environ.get("IG_APP_SECRET", ""),
         "user_id": os.environ.get("IG_USER_ID", ""), "page_name": "", "username": "", "base": ""}
    try:
        import inbox
        with inbox.db() as d:
            for k in v:
                x = inbox.get_setting(d, "ig_" + k, "")
                if x:
                    v[k] = x
            if not v["secret"]:
                v["secret"] = inbox.get_setting(d, "wa_secret", "") or os.environ.get("WA_APP_SECRET", "")
            v["app_id"] = inbox.get_setting(d, "wa_app_id", "") or os.environ.get("WA_APP_ID", "")
    except Exception as e:
        print("IG: настройки не загружены:", e, flush=True)
        if _cfg["v"]:
            return _cfg["v"]
    v.setdefault("app_id", os.environ.get("WA_APP_ID", ""))
    _cfg.update(t=time.time(), v=v)
    return v


def reset_cache():
    _cfg["t"] = 0.0


def configured():
    c = cfg()
    return bool(c["page_id"] and c["token"])


def is_ig(chat_id):
    return str(chat_id).startswith("ig:")


def igsid(chat_id):
    return str(chat_id)[3:]


def _call(method, path, token=None, **kw):
    tok = token or cfg()["token"]
    if not tok:
        raise RuntimeError("Instagram не подключён")
    r = requests.request(method, f"{GRAPH}/{path}", headers={"Authorization": "Bearer " + tok}, timeout=40, **kw)
    try:
        j = r.json()
    except Exception:
        j = {}
    if r.status_code >= 400:
        e = (j.get("error") or {})
        msg = e.get("message") or r.text[:200]
        if e.get("error_subcode") in (2534022, 2018278) or "window" in msg.lower():
            msg = "Instagram не принял сообщение: со времени последнего сообщения клиента прошло слишком много времени (менеджер может ответить в течение 7 дней). Дождитесь, когда клиент напишет снова."
        elif e.get("code") == 190:
            msg = "Токен страницы недействителен или истёк. Вставьте новый Page access token в «Ассистент → Каналы → Instagram»."
        raise RuntimeError(f"Instagram: {msg}")
    return j


# ---------- файлы: хранение в базе ----------
def _mdb():
    import inbox
    d = inbox.db()
    if not _media_ready[0]:
        blob = "BYTEA" if inbox.PG else "BLOB"
        d.run(f"CREATE TABLE IF NOT EXISTS ig_media (id TEXT PRIMARY KEY, mime TEXT, name TEXT, data {blob}, at DOUBLE PRECISION)")
        d.c.commit()
        _media_ready[0] = True
    return d


def store(data, mime, name=""):
    """Сохраняет файл, возвращает id для инбокса «ig:<ключ>». Старше 90 дней — удаляем."""
    key = secrets.token_hex(12)
    with _mdb() as d:
        d.run("DELETE FROM ig_media WHERE at < %s", (time.time() - 90 * DAY,))
        d.run("INSERT INTO ig_media (id, mime, name, data, at) VALUES (%s,%s,%s,%s,%s)", (key, mime or "application/octet-stream", name[:120], data, time.time()))
    return "ig:" + key


def download(media_id):
    """Файл из базы по id «ig:<ключ>» -> (байты, mime)."""
    with _mdb() as d:
        r = d.run("SELECT data, mime FROM ig_media WHERE id=%s", (str(media_id).replace("ig:", "", 1),), one=True)
    if not r:
        raise RuntimeError("Файл Instagram не найден (хранится 90 дней)")
    return bytes(r[0]), r[1]


def _fsig(key):
    return hmac.new((oh.HOOK_SECRET or "ig").encode(), b"igf." + key.encode(), hashlib.sha256).hexdigest()[:24]


def public_url(media_id):
    """Ссылка на наш файл, по которой Meta заберёт его для отправки клиенту."""
    key = str(media_id).replace("ig:", "", 1)
    base = (oh.PUBLIC_URL or cfg().get("base", "")).rstrip("/")
    if not base:
        raise RuntimeError("Не задан PUBLIC_URL сервера — Instagram не сможет забрать файл")
    return f"{base}/ig/f/{key}?t={_fsig(key)}"


@bp.get("/ig/f/<key>")
def media_file(key):
    if not hmac.compare_digest(request.args.get("t", ""), _fsig(key)):
        return "", 403
    try:
        data, mime = download(key)
    except Exception:
        return "", 404
    return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=3600"})


def fetch_url(url):
    """Скачивает вложение Instagram (временная ссылка CDN) -> (байты, mime)."""
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content, (r.headers.get("Content-Type") or "application/octet-stream").split(";")[0]


# ---------- отправка ----------
def last_client_at(chat):
    """Время последнего сообщения клиента в этом чате (0 — неизвестно)."""
    try:
        import inbox
        with inbox.db() as d:
            r = d.run("SELECT MAX(m.at) FROM msg m JOIN conv c ON c.id=m.conv_id WHERE c.chat_id=%s AND m.role='client'", (str(chat),), one=True)
        return float(r[0] or 0) if r else 0.0
    except Exception as e:
        print("IG: время последнего сообщения:", e, flush=True)
        return 0.0


def _window(chat, human):
    """Окно ответа: 24 ч — обычный ответ; до 7 дней — только человек, с меткой HUMAN_AGENT. ИИ вне 24 ч не отвечает."""
    last = last_client_at(chat)
    age = time.time() - last if last else 0
    if age < DAY - 60:
        return {"messaging_type": "RESPONSE"}
    if not human:
        raise RuntimeError("Instagram: прошло больше 24 часов с последнего сообщения клиента — ИИ не отвечает")
    if age > 7 * DAY - 60:
        raise RuntimeError("Instagram: прошло больше 7 дней с последнего сообщения клиента — ответить можно только после его нового сообщения.")
    return {"messaging_type": "MESSAGE_TAG", "tag": "HUMAN_AGENT"}


def _send(chat, message, human=True):
    body = {"recipient": {"id": igsid(chat)}, "message": message}
    body.update(_window(chat, human))
    return _call("POST", "me/messages", json=body)


def send_text(chat, text, human=True):
    text = (text or "").strip()
    out = None
    while text:                                    # лимит Instagram — 1000 символов, длинный ответ режем на части
        part = text[:1000]
        if len(text) > 1000:
            cut = max(part.rfind("\n"), part.rfind(". "))
            part = part[:cut + 1] if cut > 300 else part
        out = _send(chat, {"text": part.strip()}, human)
        text = text[len(part):].strip()
    return out


def send_media(chat, kind, data, name, mime, caption="", human=True):
    """kind: image | video | audio | file. Файл сохраняем у себя и отдаём Meta ссылкой. Возвращает id «ig:…» для показа в панели.
    Подписи к вложениям Instagram не поддерживает — подпись уходит отдельным сообщением."""
    mid = store(data, mime, name)
    _send(chat, {"attachment": {"type": kind, "payload": {"url": public_url(mid), "is_reusable": False}}}, human)
    if caption:
        send_text(chat, caption, human)
    return mid


def check(token=None):
    """Проверка токена: страница и привязанный Instagram-аккаунт."""
    j = _call("GET", "me", token=token, params={"fields": "id,name,instagram_business_account{id,username,name}"})
    return j


def connect(page_id, token, secret, webhook_url, verify_token, base=""):
    """«Сохранить и проверить»: проверяем токен, подписываем страницу на сообщения, сохраняем настройки."""
    import inbox
    c = cfg()
    page_id, token, secret = page_id.strip(), token.strip(), secret.strip()
    tok = token if token and "•" not in token else c["token"]
    if not tok:
        raise RuntimeError("Вставьте Page access token")
    me = check(tok)
    if page_id and me.get("id") and str(me["id"]) != page_id:
        raise RuntimeError(f"Токен принадлежит другой странице ({me.get('name', '')}, ID {me.get('id')}). Нужен токен именно страницы {page_id}.")
    page_id = page_id or str(me.get("id", ""))
    acc = me.get("instagram_business_account") or {}
    if not acc.get("id"):
        raise RuntimeError(f"К странице «{me.get('name', '')}» не привязан профессиональный аккаунт Instagram. Привяжите его в настройках Instagram (Аккаунт → Связанная страница) и повторите.")
    s = requests.post(f"{GRAPH}/{page_id}/subscribed_apps", timeout=30,
                      params={"subscribed_fields": "messages,messaging_postbacks,message_reactions", "access_token": tok})
    if s.status_code >= 400:
        try:
            m = (s.json().get("error") or {}).get("message", s.text[:200])
        except Exception:
            m = s.text[:200]
        raise RuntimeError("Не удалось подписать страницу на сообщения: " + str(m))
    sec = secret if secret and "•" not in secret else ""
    app_id, app_secret = c.get("app_id", ""), sec or c.get("secret", "")
    if app_id and app_secret:                     # подписка приложения на объект instagram (то же можно сделать вручную в кабинете Meta)
        try:
            requests.post(f"{GRAPH}/{app_id}/subscriptions", timeout=30, data={
                "object": "instagram", "callback_url": webhook_url, "verify_token": verify_token,
                "fields": "messages,messaging_postbacks,message_reactions", "access_token": f"{app_id}|{app_secret}"})
        except Exception as e:
            print("IG: подписка приложения:", e, flush=True)
    with inbox.db() as d:
        inbox.set_setting(d, "ig_page_id", page_id)
        inbox.set_setting(d, "ig_token", tok)
        if sec:
            inbox.set_setting(d, "ig_secret", sec)
        inbox.set_setting(d, "ig_user_id", str(acc.get("id", "")))
        inbox.set_setting(d, "ig_page_name", str(me.get("name", "")))
        inbox.set_setting(d, "ig_username", str(acc.get("username", "")))
        if base:
            inbox.set_setting(d, "ig_base", base)
    reset_cache()
    return {"page": me.get("name", ""), "username": acc.get("username", ""), "page_id": page_id}


def profile(sid):
    """Имя и юзернейм клиента (кэш 1 сутки)."""
    x = _names.get(sid)
    if x and time.time() - x[0] < DAY:
        return x[1], x[2]
    name, user = "", ""
    try:
        j = _call("GET", sid, params={"fields": "name,username"})
        name, user = j.get("name", "") or "", j.get("username", "") or ""
    except Exception as e:
        print("IG: профиль клиента:", e, flush=True)
    _names[sid] = (time.time(), name, user)
    return name, user


# ---------- вебхук ----------
def _sig_ok(raw):
    secret = cfg().get("secret", "")
    if not secret:
        return True                           # App Secret не задан — подпись не проверяем
    h = request.headers.get("X-Hub-Signature-256", "")
    return h.startswith("sha256=") and hmac.compare_digest(h[7:], hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest())


@bp.get("/ig/<secret>")
def verify(secret):
    """Meta проверяет адрес вебхука: отвечаем hub.challenge, если токен совпал."""
    if not oh.HOOK_SECRET or not hmac.compare_digest(secret, oh.HOOK_SECRET):
        return "", 403
    if request.args.get("hub.mode") == "subscribe" and hmac.compare_digest(request.args.get("hub.verify_token", ""), oh.HOOK_SECRET):
        return Response(request.args.get("hub.challenge", ""), mimetype="text/plain")
    return "", 403


ATT_KINDS = {"image", "video", "audio", "file", "story_mention", "ig_reel", "reel", "share", "animated_image"}


def parse(payload):
    """Сообщения из тела вебхука Meta (object=instagram) -> список словарей. Наши исходящие (is_echo), реакции и прочтения пропускаем."""
    out = []
    if payload.get("object") != "instagram":
        return out
    for e in payload.get("entry", []):
        for ev in e.get("messaging", []) or []:
            sid = str((ev.get("sender") or {}).get("id", ""))
            m = ev.get("message")
            if ev.get("postback"):
                p = ev["postback"]
                out.append({"id": p.get("mid") or f"pb:{sid}:{ev.get('timestamp')}", "sid": sid, "text": p.get("title") or p.get("payload") or "", "atts": []})
                continue
            if not m or m.get("is_echo") or m.get("is_deleted") or not sid:
                continue
            note = ""
            rt = m.get("reply_to") or {}
            if rt.get("story"):
                note = "↩️ Ответ на вашу историю"
            atts = []
            for a in m.get("attachments") or []:
                t, url = a.get("type", ""), (a.get("payload") or {}).get("url", "")
                if t == "story_mention":
                    note = "📣 Клиент упомянул вас в своей истории"
                if t in ATT_KINDS and url:
                    atts.append({"type": t, "url": url})
                elif t:
                    atts.append({"type": t, "url": ""})
            text = m.get("text", "") or ""
            if m.get("is_unsupported") and not text and not atts:
                text = "[сообщение такого типа не поддерживается]"
            if note:
                text = note + (f"\n{text}" if text else "")
            out.append({"id": m.get("mid", ""), "sid": sid, "text": text, "atts": atts})
    return out


def _kind(t, mime):
    if t in ("image", "animated_image", "story_mention") or (t in ("share", "ig_reel", "reel") and mime.startswith("image/")):
        return "image"
    if t == "audio" or mime.startswith("audio/"):
        return "audio"
    if t in ("video", "ig_reel", "reel") or mime.startswith("video/"):
        return "video"
    return "file"


def handle(m):
    """Одно входящее сообщение -> инбокс (так же, как из WhatsApp и Telegram)."""
    import inbox
    chat, text, photo, voice, pdf, att = "ig:" + m["sid"], m["text"], None, None, None, None
    extra = []
    for a in m["atts"]:
        if not a["url"]:
            if a["type"] in ("like_heart",):
                extra.append("❤️")
            elif a["type"] not in ("fallback",):
                extra.append(f"[вложение: {a['type']}]")
            continue
        try:
            data, mime = fetch_url(a["url"])
        except Exception as e:
            print("IG: вложение не скачано:", e, flush=True)
            extra.append("[вложение не удалось загрузить]")
            continue
        k = _kind(a["type"], mime)
        name = a["url"].split("?")[0].rsplit("/", 1)[-1][:80] or "файл"
        mid = store(data, mime, name)
        if k == "image" and not photo:
            photo = mid
        elif k == "audio" and not voice and not att:
            voice = mid
            att = {"t": "voice", "id": mid, "dur": 0}
        elif k == "video" and not att:
            att = {"t": "video", "id": mid, "dur": 0}
            extra.append("🎥 Видео" if a["type"] == "video" else "🎥 Reels")
        elif not att:
            att = {"t": "doc", "id": mid, "name": name, "size": len(data), "mime": mime}
            if mime == "application/pdf" or name.lower().endswith(".pdf"):
                pdf = {"id": mid, "name": name, "size": len(data)}
            else:
                extra.append(f"📎 Файл «{name}»")
    if extra:
        text = "\n".join(extra + ([text] if text else []))
    name, user = profile(m["sid"])
    inbox.on_client_message(chat, {"first_name": name or (("@" + user) if user else "Instagram"), "username": user}, text, photo, voice, pdf, att)


@bp.post("/ig/<secret>")
def webhook(secret):
    if not oh.HOOK_SECRET or not hmac.compare_digest(secret, oh.HOOK_SECRET):
        return "", 403
    raw = request.get_data()
    if not _sig_ok(raw):
        return "", 403
    try:
        msgs = parse(json.loads(raw or b"{}"))
    except Exception as e:
        print("IG: тело вебхука не разобрано:", e, flush=True)
        return "", 200
    now = time.time()
    for k in [k for k, t in _seen.items() if now - t > 3600]:
        _seen.pop(k, None)
    for m in msgs:
        if not m["id"] or m["id"] in _seen:
            continue
        _seen[m["id"]] = now
        threading.Thread(target=handle, args=(m,), daemon=True).start()
    return "", 200
