"""Чат на сайте (amura.kz и amura.kz/shop): сообщения попадают в панель → «Сообщения» как канал «Сайт».

Посетитель получает токен «<id>.<подпись>» (хранится в браузере). Диалог в панели — chat_id «web:<id>».
Сообщение клиента идёт тем же путём, что WhatsApp / Telegram / Instagram (inbox.on_client_message): ИИ отвечает
или передаёт менеджеру. Ответы ИИ и менеджера сохраняются в базе, сайт забирает их опросом (/chat/poll).
Фото и файлы хранятся у нас (таблица webchat_file) и отдаются по ссылке только владельцу токена или в панель.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from flask import Blueprint, Response, jsonify, request

import order_hook as oh

bp = Blueprint("webchat", __name__)
bp.after_request(oh.cors)                 # сайт amura.kz обращается к серверу с другого адреса
PREFIX = "web:"
MAX_FILE = 10 * 1024 * 1024
_SECRET = (os.environ.get("WEBCHAT_SECRET") or hashlib.sha256((oh.MS_TOKEN or "amura") .encode() + b":webchat").hexdigest()).encode()
_ready = [False]
_hits = {}
_hits_lock = threading.Lock()


def is_web(chat):
    return str(chat).startswith(PREFIX)


def _sig(s):
    return hmac.new(_SECRET, s.encode(), hashlib.sha256).hexdigest()[:24]


def new_token():
    cid = secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
    return f"{cid}.{_sig(cid)}"


def chat_of(token):
    """«web:<id>» для верного токена, иначе None."""
    cid, _, sig = str(token or "").partition(".")
    if not re.fullmatch(r"[A-Za-z0-9]{8,32}", cid) or not hmac.compare_digest(sig, _sig(cid)):
        return None
    return PREFIX + cid


def db():
    import inbox
    d = inbox.db()
    if not _ready[0]:
        blob = "BYTEA" if inbox.PG else "BLOB"
        d.run(f"CREATE TABLE IF NOT EXISTS webchat_file (id TEXT PRIMARY KEY, chat_id TEXT, mime TEXT, name TEXT, data {blob}, at DOUBLE PRECISION)")
        d.c.commit()
        _ready[0] = True
    return d


def store(chat, data, mime, name):
    fid = PREFIX + secrets.token_hex(12)
    with db() as d:
        d.run("INSERT INTO webchat_file (id, chat_id, mime, name, data, at) VALUES (%s,%s,%s,%s,%s,%s)",
              (fid, chat, mime, name[:120], data, time.time()))
    return fid


def download(file_id):
    """(байты, mime) — как wa.download / ig.download, для панели."""
    with db() as d:
        r = d.run("SELECT data, mime FROM webchat_file WHERE id=%s", (file_id,), one=True)
    if not r:
        raise RuntimeError("файл не найден")
    return bytes(r[0]), r[1] or "application/octet-stream"


def _conv(d, chat):
    r = d.run("SELECT id FROM conv WHERE chat_id=%s", (chat,), one=True)
    return r[0] if r else None


def save_pdf(chat, name, pdf, caption):
    """Накладная от ИИ-продажника: в чат сайта — файлом (send_pdf для канала «Сайт»)."""
    import inbox
    fid = store(chat, pdf, "application/pdf", name)
    with inbox.db() as d:
        cid = _conv(d, chat)
        if cid:
            inbox.save_msg(d, cid, "ai", caption, media={"t": "doc", "id": fid, "name": name, "size": len(pdf), "mime": "application/pdf"})


def _limited(ip, limit=30, window=600):
    now = time.time()
    with _hits_lock:
        h = [t for t in _hits.get(ip, []) if now - t < window] + [now]
        _hits[ip] = h
        if len(_hits) > 5000:
            for k in [k for k, v in _hits.items() if now - v[-1] > window]:
                del _hits[k]
        return len(h) > limit


def _ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


# ---------- API для сайта ----------
@bp.route("/chat/start", methods=["POST", "OPTIONS"])
def start():
    if request.method == "OPTIONS":
        return "", 204
    if _limited(_ip(), limit=20):
        return jsonify(ok=False, error="Слишком часто, попробуйте через несколько минут"), 429
    return jsonify(ok=True, token=new_token())


@bp.route("/chat/send", methods=["POST", "OPTIONS"])
def send():
    if request.method == "OPTIONS":
        return "", 204
    form = request.form if request.files else (request.get_json(silent=True) or {})
    chat = chat_of(form.get("token"))
    if not chat:
        return jsonify(ok=False, error="Обновите страницу и напишите ещё раз"), 403
    if _limited(_ip()):
        return jsonify(ok=False, error="Слишком много сообщений подряд, подождите немного"), 429
    text = str(form.get("text", "")).strip()[:2000]
    name = re.sub(r"\s+", " ", str(form.get("name", ""))).strip()[:60]
    phone = re.sub(r"\D", "", str(form.get("phone", "")))[:15]
    photo = None
    f = request.files.get("file")
    if f:
        data = f.read()
        if len(data) > MAX_FILE:
            return jsonify(ok=False, error="Файл больше 10 МБ"), 413
        mime = f.mimetype or "application/octet-stream"
        if not (mime.startswith("image/") or mime == "application/pdf"):
            return jsonify(ok=False, error="Можно прислать фото или PDF"), 400
        fid = store(chat, data, mime, f.filename or "file")
        if mime.startswith("image/"):
            photo = fid
        else:
            text = (text + "\n" if text else "") + f"📄 PDF «{(f.filename or 'файл')[:60]}»"
    if not text and not photo:
        return jsonify(ok=False, error="Напишите сообщение"), 400
    if len(phone) == 11 and phone[0] == "8":
        phone = "7" + phone[1:]
    who = name or "Посетитель сайта"
    if phone:
        who += f" +{phone}"
    import inbox
    threading.Thread(target=inbox.on_client_message, args=(chat, {"first_name": who, "username": ""}, text),
                     kwargs={"photo": photo}, daemon=True).start()
    return jsonify(ok=True)


@bp.get("/chat/poll")
def poll():
    chat = chat_of(request.args.get("token"))
    if not chat:
        return jsonify(ok=False, error="нет доступа"), 403
    try:
        after = int(request.args.get("after", 0))
    except ValueError:
        after = 0
    import inbox
    with inbox.db() as d:
        cid = _conv(d, chat)
        rows = d.run("SELECT id, role, text, photo, media, at, edited FROM msg WHERE conv_id=%s AND id>%s ORDER BY id LIMIT 100",
                     (cid, after), many=True) if cid else []
    tok = request.args.get("token")
    out = []
    for i, role, text, photo, media, at, edited in rows or []:
        m = json.loads(media) if media else None
        item = {"id": i, "me": role == "client", "text": text or "", "at": at, "ed": 1 if edited else 0}
        if photo and str(photo).startswith(PREFIX):
            item["photo"] = f"{oh.PUBLIC_URL}/chat/f/{photo}?token={tok}"
        if m and str(m.get("id", "")).startswith(PREFIX):
            item["file"] = {"url": f"{oh.PUBLIC_URL}/chat/f/{m['id']}?token={tok}", "name": m.get("name") or "файл", "t": m.get("t")}
        out.append(item)
    try:
        ed = float(request.args.get("ed", 0))
    except ValueError:
        ed = 0
    with inbox.db() as d:                          # исправленные менеджером сообщения (ed — последняя правка, что сайт уже видел)
        edits = d.run("SELECT id, text, edited FROM msg WHERE conv_id=%s AND edited>%s ORDER BY edited LIMIT 100",
                      (cid, ed), many=True) if cid else []
    return jsonify(ok=True, messages=out, edits=[{"id": i, "text": t or "", "ed": e} for i, t, e in edits or []])


@bp.get("/chat/f/<path:file_id>")
def file(file_id):
    chat = chat_of(request.args.get("token"))
    with db() as d:
        r = d.run("SELECT data, mime, name, chat_id FROM webchat_file WHERE id=%s", (file_id,), one=True)
    if not chat or not r or r[3] != chat:
        return "", 404
    return Response(bytes(r[0]), mimetype=r[1] or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=86400", "Content-Disposition": f"inline; filename=\"{re.sub(r'[^A-Za-z0-9._-]', '_', r[2] or 'file')}\""})
