"""WhatsApp Business (официальный Cloud API от Meta): приём и отправка сообщений для инбокса.
Настройки (Phone number ID, токен, App Secret) вводятся в панели «Обзор → Каналы» и хранятся в базе; можно и через env WA_PHONE_ID / WA_TOKEN / WA_APP_SECRET.
Чат в инбоксе имеет chat_id вида «wa:77001234567»."""
import hashlib
import hmac
import json
import os
import threading
import time

import requests
from flask import Blueprint, Response, jsonify, request

import order_hook as oh

bp = Blueprint("wa", __name__)
GRAPH_VER = "v21.0"
GRAPH = "https://graph.facebook.com/" + GRAPH_VER
_seen = {}                                   # id входящих сообщений: Meta повторяет доставку, дубли не нужны
_cfg = {"t": 0.0, "v": {}}


def cfg():
    """Настройки WhatsApp (кэш 30 с): база, затем env."""
    if time.time() - _cfg["t"] < 30:
        return _cfg["v"]
    v = {"phone_id": os.environ.get("WA_PHONE_ID", ""), "token": os.environ.get("WA_TOKEN", ""), "secret": os.environ.get("WA_APP_SECRET", ""),
         "app_id": os.environ.get("WA_APP_ID", ""), "config_id": os.environ.get("WA_CONFIG_ID", ""), "config_coex": os.environ.get("WA_CONFIG_COEX", ""), "waba": ""}
    try:
        import inbox
        with inbox.db() as d:
            for k in v:
                x = inbox.get_setting(d, "wa_" + k, "")
                if x:
                    v[k] = x
    except Exception as e:
        print("WA: настройки не загружены:", e, flush=True)
        if _cfg["v"]:
            return _cfg["v"]
    _cfg.update(t=time.time(), v=v)
    return v


def reset_cache():
    _cfg["t"] = 0.0


def configured():
    c = cfg()
    return bool(c["phone_id"] and c["token"])


def is_wa(chat_id):
    return str(chat_id).startswith("wa:")


def number(chat_id):
    return str(chat_id)[3:]


def _call(method, path, **kw):
    c = cfg()
    if not configured():
        raise RuntimeError("WhatsApp не подключён")
    r = requests.request(method, f"{GRAPH}/{path}", headers={"Authorization": "Bearer " + c["token"]}, timeout=40, **kw)
    try:
        j = r.json()
    except Exception:
        j = {}
    if r.status_code >= 400:
        e = (j.get("error") or {})
        code = e.get("code")
        msg = e.get("message") or r.text[:200]
        if code in (131047, 131026) or "24" in str(e.get("error_data", "")):
            msg = "Прошло больше 24 часов с последнего сообщения клиента: WhatsApp разрешает ответить только одобренным шаблоном."
        raise RuntimeError(f"WhatsApp: {msg}")
    return j


def send_text(chat, text):
    c = cfg()
    return _call("POST", f"{c['phone_id']}/messages", json={"messaging_product": "whatsapp", "to": number(chat), "type": "text",
                                                           "text": {"body": text[:4000], "preview_url": True}})


def upload(data, name, mime):
    c = cfg()
    j = _call("POST", f"{c['phone_id']}/media", data={"messaging_product": "whatsapp", "type": mime}, files={"file": (name, data, mime)})
    return j["id"]


def send_media(chat, kind, data, name, mime, caption=""):
    """kind: image | document | audio | video. Возвращает id медиа (для показа в панели)."""
    c = cfg()
    mid = upload(data, name, mime)
    body = {"id": mid}
    if kind in ("image", "video", "document") and caption:
        body["caption"] = caption[:1000]
    if kind == "document":
        body["filename"] = name[:200]
    _call("POST", f"{c['phone_id']}/messages", json={"messaging_product": "whatsapp", "to": number(chat), "type": kind, kind: body})
    return mid


def download(media_id):
    """Файл из WhatsApp по id -> (байты, mime)."""
    info = _call("GET", media_id.replace("wa:", "", 1))
    r = requests.get(info["url"], headers={"Authorization": "Bearer " + cfg()["token"]}, timeout=60)
    r.raise_for_status()
    return r.content, info.get("mime_type", "application/octet-stream")


def mark_read(msg_id):
    try:
        c = cfg()
        _call("POST", f"{c['phone_id']}/messages", json={"messaging_product": "whatsapp", "status": "read", "message_id": msg_id})
    except Exception:
        pass


def check():
    """Проверка настроек: имя и номер из кабинета Meta."""
    c = cfg()
    return _call("GET", f"{c['phone_id']}", params={"fields": "display_phone_number,verified_name,quality_rating"})


# ---------- подключение «одной кнопкой» (Embedded Signup) ----------
def connect(code, phone_id, waba_id, webhook_url, verify_token, coex=False):
    """Окно Facebook вернуло code и id аккаунта (и номера): меняем code на токен, подписываем вебхук, регистрируем номер, сохраняем настройки.
    coex=True — номер остаётся в приложении WhatsApp Business на телефоне и одновременно работает через API (сосуществование)."""
    import inbox
    c = cfg()
    if not (c["app_id"] and c["secret"]):
        raise RuntimeError("Сначала сохраните App ID и App Secret приложения Meta")
    r = requests.get(f"{GRAPH}/oauth/access_token", params={"client_id": c["app_id"], "client_secret": c["secret"], "code": code}, timeout=30)
    j = r.json()
    if r.status_code >= 400 or not j.get("access_token"):
        raise RuntimeError("Meta не выдала токен: " + str((j.get("error") or {}).get("message", r.text[:200])))
    token = j["access_token"]
    h = {"Authorization": "Bearer " + token}
    if not phone_id:                                        # при сосуществовании окно возвращает только аккаунт: номер берём из списка
        pn = requests.get(f"{GRAPH}/{waba_id}/phone_numbers", headers=h, params={"fields": "id,display_phone_number"}, timeout=30).json()
        rows = pn.get("data") or []
        if not rows:
            raise RuntimeError("В аккаунте WhatsApp не найден номер. Дойдите в окне Facebook до конца и повторите.")
        phone_id = rows[0]["id"]
    # подписка приложения на события, включая сообщения, написанные с телефона (smb_message_echoes) — на уровне приложения
    try:
        requests.post(f"{GRAPH}/{c['app_id']}/subscriptions", timeout=30, data={
            "object": "whatsapp_business_account", "callback_url": webhook_url, "verify_token": verify_token,
            "fields": "messages,smb_message_echoes", "access_token": f"{c['app_id']}|{c['secret']}"})
    except Exception as e:
        print("WA: подписка приложения:", e, flush=True)
    s = requests.post(f"{GRAPH}/{waba_id}/subscribed_apps", headers=h, timeout=30,
                      json={"override_callback_uri": webhook_url, "verify_token": verify_token})
    if s.status_code >= 400:
        raise RuntimeError("Не удалось подписать аккаунт на сообщения: " + str((s.json().get("error") or {}).get("message", s.text[:200])))
    if not coex:                                            # у номера из приложения регистрация не нужна (он уже зарегистрирован)
        try:
            requests.post(f"{GRAPH}/{phone_id}/register", headers=h, timeout=30,
                          json={"messaging_product": "whatsapp", "pin": f"{int.from_bytes(os.urandom(3), 'big') % 900000 + 100000}"})
        except Exception as e:
            print("WA: регистрация номера:", e, flush=True)
    with inbox.db() as d:
        inbox.set_setting(d, "wa_token", token)
        inbox.set_setting(d, "wa_phone_id", phone_id)
        inbox.set_setting(d, "wa_waba", waba_id)
    reset_cache()
    return check()


# ---------- вебхук ----------
def _sig_ok(raw):
    secret = cfg().get("secret", "")
    if not secret:
        return True                           # App Secret не задан — подпись не проверяем
    h = request.headers.get("X-Hub-Signature-256", "")
    return h.startswith("sha256=") and hmac.compare_digest(h[7:], hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest())


@bp.get("/wa/<secret>")
def verify(secret):
    """Meta проверяет адрес вебхука: отвечаем hub.challenge, если токен совпал."""
    if not oh.HOOK_SECRET or not hmac.compare_digest(secret, oh.HOOK_SECRET):
        return "", 403
    if request.args.get("hub.mode") == "subscribe" and hmac.compare_digest(request.args.get("hub.verify_token", ""), oh.HOOK_SECRET):
        return Response(request.args.get("hub.challenge", ""), mimetype="text/plain")
    return "", 403


def parse(payload):
    """Сообщения из тела вебхука Meta -> список словарей."""
    out = []
    for e in payload.get("entry", []):
        for ch in e.get("changes", []):
            v = ch.get("value", {})
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in v.get("contacts", [])}
            for m in v.get("message_echoes", []):                      # менеджер написал клиенту с телефона (сосуществование)
                t = m.get("type")
                body = (m.get("text") or {}).get("body", "") if t == "text" else (f"[{t}]" if t else "")
                out.append({"echo": True, "id": m.get("id"), "phone": m.get("to", ""), "name": "", "type": t, "text": body, "media": None})
            for m in v.get("messages", []):
                t = m.get("type")
                x = {"id": m.get("id"), "phone": m.get("from", ""), "name": names.get(m.get("from"), ""), "type": t, "text": "", "media": None}
                if t == "text":
                    x["text"] = (m.get("text") or {}).get("body", "")
                elif t in ("image", "audio", "video", "document", "sticker"):
                    d = m.get(t) or {}
                    x.update(media={"id": "wa:" + d.get("id", ""), "mime": d.get("mime_type", ""), "name": d.get("filename", "")}, text=d.get("caption", ""))
                    x["voice"] = bool(d.get("voice"))
                elif t == "location":
                    l = m.get("location") or {}
                    x["text"] = f"📍 Геолокация: {l.get('latitude')}, {l.get('longitude')} {l.get('name', '')}".strip()
                elif t == "button":
                    x["text"] = (m.get("button") or {}).get("text", "")
                elif t == "interactive":
                    i = m.get("interactive") or {}
                    x["text"] = ((i.get("button_reply") or i.get("list_reply") or {}).get("title", ""))
                elif t == "reaction":
                    continue
                else:
                    x["text"] = "[сообщение такого типа не поддерживается]"
                out.append(x)
    return out


def handle(m):
    """Одно входящее сообщение -> инбокс (так же, как из Telegram)."""
    import inbox
    if m.get("echo"):
        return inbox.on_manager_echo("wa:" + m["phone"], m["text"])
    chat, med, text, photo, voice, pdf, att = "wa:" + m["phone"], m["media"], m["text"], None, None, None, None
    if med:
        mime, t = med["mime"], m["type"]
        if t == "image" or (t == "sticker"):
            photo = med["id"]
        elif t == "audio":
            voice = med["id"]
            att = {"t": "voice", "id": med["id"], "dur": 0}
        elif t == "video":
            att = {"t": "video", "id": med["id"], "dur": 0}
            text = "🎥 Видео" + (f"\n{text}" if text else "")
        else:
            name = med["name"] or "файл"
            att = {"t": "doc", "id": med["id"], "name": name, "size": 0, "mime": mime}
            if mime == "application/pdf" or name.lower().endswith(".pdf"):
                pdf = {"id": med["id"], "name": name, "size": 0}
            else:
                text = f"📎 Файл «{name}»" + (f"\n{text}" if text else "")
    inbox.on_client_message(chat, {"first_name": m["name"] or "+" + m["phone"], "username": ""}, text, photo, voice, pdf, att)


@bp.post("/wa/<secret>")
def webhook(secret):
    if not oh.HOOK_SECRET or not hmac.compare_digest(secret, oh.HOOK_SECRET):
        return "", 403
    raw = request.get_data()
    if not _sig_ok(raw):
        return "", 403
    try:
        msgs = parse(json.loads(raw or b"{}"))
    except Exception as e:
        print("WA: тело вебхука не разобрано:", e, flush=True)
        return "", 200
    now = time.time()
    for k in [k for k, t in _seen.items() if now - t > 3600]:
        _seen.pop(k, None)
    for m in msgs:
        if not m["id"] or m["id"] in _seen:
            continue
        _seen[m["id"]] = now
        threading.Thread(target=lambda m=m: ((mark_read(m["id"]) if not m.get("echo") else None), handle(m)), daemon=True).start()
    return "", 200
