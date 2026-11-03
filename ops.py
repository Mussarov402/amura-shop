"""Связь с сервером AMURA на Hoster.kz (переезд, вариант В — решение владельца 03.11.2026).

/ops/hello  — сервер присылает публичные ключи и отпечаток: только в журнал Render (их закрепляют PR-ом в deploy/server-key.json).
/ops/report — отчёт сервера, подписанный его ключом. Принимается только с закреплённым ключом; ok и alert — владельцу
              в те же Telegram-уведомления (раздела «Сервер» в системе AMURA нет — слово владельца).
Секретов здесь нет: ключи сервера публичные, секретные части остаются на сервере.
"""
import base64
import json
import os
import time

from flask import Blueprint, jsonify, request

import order_hook as oh

bp = Blueprint("ops", __name__)
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy", "server-key.json")
_seen = {}


def pinned():
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            k = json.load(f)
        return k if k.get("sign") and k.get("fpr") else None
    except (OSError, ValueError):
        return None


def verify(body, sig, key):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key["sign"])).verify(base64.b64decode(sig or ""), body)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


@bp.post("/ops/hello")
def hello():
    if oh.too_many("ops:" + oh.client_ip(), limit=30, window=600):
        return jsonify(ok=False), 429
    d = request.get_json(silent=True) or {}
    k = pinned()
    known = bool(k and d.get("fpr") == k["fpr"])
    if not known:                                   # новый сервер — в журнал: отпечаток и ключи для закрепления
        print(f"Сервер Hoster: hello {str(d.get('host', ''))[:60]} ip {oh.client_ip()} fpr {str(d.get('fpr', ''))[:16]} "
              f"sign {str(d.get('sign', ''))[:60]} box {str(d.get('box', ''))[:60]}", flush=True)
    return jsonify(ok=True, pinned=known)


@bp.post("/ops/report")
def report():
    k = pinned()
    body = request.get_data()
    if not k or not verify(body, request.headers.get("X-Amura-Sig"), k):
        return jsonify(ok=False, error="ключ сервера не закреплён"), 403
    d = json.loads(body or b"{}")
    if abs(time.time() - float(d.get("t") or 0)) > 600:
        return jsonify(ok=False, error="старый отчёт"), 400
    level, text = str(d.get("level", "info")), str(d.get("text", ""))[:3500]
    print(f"Сервер Hoster [{level}]: {text}", flush=True)
    if level in ("alert", "ok") and oh.OWNER:
        icon = "🚨 Сервер AMURA (Hoster.kz): " if level == "alert" else "🖥 Сервер AMURA (Hoster.kz): "
        key = f"ops:{level}:{text[:60]}"
        if time.time() - _seen.get(key, 0) > (600 if level == "alert" else 0):
            _seen[key] = time.time()
            oh.notify_bg(lambda: oh.tg("sendMessage", chat_id=oh.OWNER, text=icon + text))
    return jsonify(ok=True)
