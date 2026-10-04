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
def connect(code, phone_id, waba_id, webhook_url, verify_token, coex=False, redirect_uris=None):
    """Окно Facebook вернуло code и id аккаунта (и номера): меняем code на токен, подписываем вебхук, регистрируем номер, сохраняем настройки.
    coex=True — номер остаётся в приложении WhatsApp Business на телефоне и одновременно работает через API (сосуществование)."""
    import inbox
    c = cfg()
    if not (c["app_id"] and c["secret"]):
        raise RuntimeError("Сначала сохраните App ID и App Secret приложения Meta")
    token, errs = "", []
    for ru in (redirect_uris or [""]):                      # код из окна Facebook привязан к redirect_uri: пробуем пустой, адрес страницы и т.д.
        p = {"client_id": c["app_id"], "client_secret": c["secret"], "code": code}
        if ru is not None:
            p["redirect_uri"] = ru
        r = requests.get(f"{GRAPH}/oauth/access_token", params=p, timeout=30)
        j = r.json()
        if r.status_code < 400 and j.get("access_token"):
            token = j["access_token"]
            break
        m = str((j.get("error") or {}).get("message", r.text[:200]))
        lab = "пусто" if ru == "" else "без адреса" if ru is None else ru.replace("https://", "")[:42]
        errs.append(f"[{lab}] {m[:34]}")
    if not token:
        hint = ""
        if any("domain" in e.lower() for e in errs):
            from urllib.parse import urlparse
            hint = f" Добавьте {urlparse(webhook_url).netloc} в поле «Домены приложения» (Настройки приложения → Основные), нажмите «Сохранить изменения» и повторите."
        raise RuntimeError("Meta не выдала токен: " + " | ".join(errs)[:700] + hint)
    return finish(token, phone_id, waba_id, webhook_url, verify_token, coex)


def connect_token(token, phone_id, webhook_url, verify_token):
    """Запасной путь без кода из окна Facebook: токен системного пользователя (создаётся в Business Settings)."""
    c = cfg()
    if not (c["app_id"] and c["secret"]):
        raise RuntimeError("Сначала сохраните App ID и App Secret приложения Meta")
    return finish(token.strip(), phone_id.strip(), "", webhook_url, verify_token, coex=True)


def finish(token, phone_id, waba_id, webhook_url, verify_token, coex):
    import inbox
    c = cfg()
    h = {"Authorization": "Bearer " + token}
    wabas = [waba_id] if waba_id else []
    if not wabas:                                           # окно Facebook не прислало аккаунт (iPhone) или вход по токену: берём аккаунты из самого токена
        dt = requests.get(f"{GRAPH}/debug_token", params={"input_token": token, "access_token": f"{c['app_id']}|{c['secret']}"}, timeout=30).json()
        if (dt.get("data") or {}).get("is_valid") is False:
            raise RuntimeError("Токен недействителен: " + str((dt["data"].get("error") or {}).get("message", "")))
        bizs = []
        for g in (dt.get("data") or {}).get("granular_scopes", []):
            if g.get("scope") in ("whatsapp_business_management", "whatsapp_business_messaging"):
                wabas += [t for t in g.get("target_ids", []) if t not in wabas]
            elif g.get("scope") == "business_management":
                bizs += [t for t in g.get("target_ids", []) if t not in bizs]
        if not wabas:                                       # токен администратора (системный пользователь с ролью Admin) видит всё через компанию
            try:
                me = requests.get(f"{GRAPH}/me", headers=h, params={"fields": "business"}, timeout=30).json()
                if (me.get("business") or {}).get("id") and me["business"]["id"] not in bizs:
                    bizs.append(me["business"]["id"])
            except Exception as e:
                print("WA: компания токена:", e, flush=True)
            for biz in bizs:
                for edge in ("owned_whatsapp_business_accounts", "client_whatsapp_business_accounts"):
                    try:
                        r = requests.get(f"{GRAPH}/{biz}/{edge}", headers=h, params={"fields": "id,name", "limit": 50}, timeout=30).json()
                        wabas += [x["id"] for x in (r.get("data") or []) if x["id"] not in wabas]
                    except Exception as e:
                        print("WA: аккаунты компании:", e, flush=True)
        if not wabas:
            raise RuntimeError("К токену не привязан ни один аккаунт WhatsApp, и компания их тоже не показала. В Business Settings добавьте ваш аккаунт WhatsApp в доступы этого системного пользователя («Добавить объекты» → «Аккаунты WhatsApp» → «Полный контроль») и создайте токен заново.")
    if not phone_id:                                        # номер: из списка аккаунта; тестовый номер Meta (+1 555…) берём только если других нет
        found = []
        for wb in wabas:
            pn = requests.get(f"{GRAPH}/{wb}/phone_numbers", headers=h, params={"fields": "id,display_phone_number"}, timeout=30).json()
            found += [(wb, r["id"], r.get("display_phone_number", "")) for r in (pn.get("data") or [])]
        if not found:
            raise RuntimeError("В аккаунте WhatsApp не найден номер. Дойдите в окне Facebook до конца и повторите.")
        real = [x for x in found if not x[2].replace(" ", "").replace("-", "").startswith("+1555")]
        waba_id, phone_id, _ = (real or found)[0]
    elif not waba_id:
        waba_id = wabas[0]
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


# ---------- юридические страницы (нужны Meta для приложения: политика конфиденциальности и удаление данных) ----------
_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} — AMURA Cosmetics</title>
<style>body{{margin:0;background:#f5f6f4;color:#16201b;font:16px/1.6 Manrope,system-ui,sans-serif}}main{{max-width:760px;margin:0 auto;padding:28px 18px 60px}}h1{{font-size:26px;margin:0 0 6px}}h2{{font-size:18px;margin:26px 0 6px}}.m{{color:#66726b;font-size:14px}}a{{color:#0e3b2c}}li{{margin:4px 0}}</style></head><body><main>
<div class="m">AMURA Cosmetics · оптовая корейская косметика, Казахстан</div><h1>{title}</h1><div class="m">Редакция от 3 октября 2026</div>{body}</main></body></html>"""

_PRIVACY = """
<h2>1. Кто мы</h2><p>AMURA Cosmetics (далее «мы») — оптовый магазин корейской косметики в Казахстане. Сайт и сервис заказов: amura-shop-api.onrender.com. По вопросам данных пишите в WhatsApp или Telegram на номер +7 700 367 13 50.</p>
<h2>2. Какие данные мы получаем</h2><ul><li>Имя, номер телефона, город, адрес и способ доставки — когда вы оформляете заказ на сайте или в чате.</li><li>Ваши сообщения, фото, голосовые и файлы, которые вы присылаете нам в Telegram или WhatsApp, а также имя и номер из профиля мессенджера.</li><li>История ваших заказов.</li></ul>
<h2>3. Зачем мы их используем</h2><ul><li>Чтобы оформить и доставить заказ, выставить накладную и принять оплату.</li><li>Чтобы отвечать на ваши вопросы о товарах, ценах и наличии: отвечает наш менеджер или автоматический помощник.</li><li>Чтобы связаться с вами по заказу.</li></ul>
<h2>4. Кому мы передаём данные</h2><p>Мы не продаём данные. Для работы сервиса мы используем: МойСклад (учёт заказов и клиентов), Telegram и WhatsApp / Meta Platforms (приём и отправка сообщений), OpenAI (подготовка автоматических ответов и расшифровка голосовых: передаётся только текст или аудио вашего сообщения), Render (хранение данных сервиса), транспортные компании (имя, телефон, адрес получателя для доставки).</p>
<h2>5. Сколько хранятся данные</h2><p>Данные заказов — столько, сколько нужно для учёта и в пределах требований законодательства Республики Казахстан. Переписку — пока она нужна для обслуживания, затем удаляем по запросу.</p>
<h2>6. Ваши права</h2><p>Вы можете запросить, какие данные о вас у нас есть, исправить их или потребовать удаления. Как это сделать — на странице <a href="/data-deletion">«Удаление данных»</a>.</p>
<h2>7. Безопасность</h2><p>Доступ к данным ограничен сотрудниками, доступ к панели управления защищён входом через Telegram с одноразовым кодом. Передача данных идёт по защищённому соединению.</p>
<h2>8. Изменения</h2><p>Мы можем обновлять эту политику. Актуальная версия всегда по этому адресу.</p>"""

_DELETION = """
<h2>Как удалить ваши данные</h2><p>Если вы хотите, чтобы мы удалили данные о вас (переписку, контактные данные, профиль клиента), сделайте любое из двух:</p>
<ol><li>Напишите нам в WhatsApp или Telegram на номер <b>+7 700 367 13 50</b> сообщение «Удалите мои данные» с того аккаунта, с которого вы нам писали.</li><li>Отправьте это сообщение боту заказов AMURA Orders в Telegram.</li></ol>
<p>Мы удалим переписку и контактные данные в течение 30 дней и подтвердим это вам в том же чате.</p>
<h2>Что мы не можем удалить</h2><p>Данные уже оформленных заказов и накладных мы обязаны хранить для бухгалтерского и налогового учёта по законодательству Республики Казахстан. Они не используются для рекламы и не передаются третьим лицам, кроме перевозчиков и учётной системы.</p>
<p><a href="/privacy">Политика конфиденциальности</a></p>"""


@bp.get("/privacy")
def privacy():
    return Response(_PAGE.format(title="Политика конфиденциальности", body=_PRIVACY), mimetype="text/html; charset=utf-8")


@bp.get("/data-deletion")
def data_deletion():
    return Response(_PAGE.format(title="Удаление данных", body=_DELETION), mimetype="text/html; charset=utf-8")
