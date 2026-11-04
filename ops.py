"""Связь с сервером AMURA на Hoster.kz (переезд, вариант В — решение владельца 03.11.2026).

/ops/hello  — сервер присылает публичные ключи и отпечаток: только в журнал Render (их закрепляют PR-ом в deploy/server-key.json).
/ops/report — отчёт сервера, подписанный его ключом. Принимается только с закреплённым ключом; ok и alert — владельцу
              в те же Telegram-уведомления (раздела «Сервер» в системе AMURA нет — слово владельца).
/ops/env    — переменные приложения (ключи МойСклад, площадок и т. д.) для переезда: запрос подписан ключом сервера,
              ответ зашифрован его публичным ключом box (X25519 + HKDF-SHA256 + AES-GCM) — прочитать может только этот сервер.
Секретов в коде и в чате нет: ключи сервера публичные, секретные части остаются на сервере.
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


SKIP_ENV = ("PATH", "HOME", "HOSTNAME", "PWD", "OLDPWD", "SHLVL", "LANG", "LC_ALL", "TERM", "USER", "_", "PORT", "PYTHONPATH",
            "GUNICORN_CMD_ARGS", "WEB_CONCURRENCY", "AMURA_MASTER_PID", "SERVER_SOFTWARE", "VIRTUAL_ENV", "PYTHONUNBUFFERED")


def seal(box_pub_b64, data):
    """Зашифровать для сервера: эфемерный X25519 → общий секрет → HKDF-SHA256 → AES-GCM. → {eph, nonce, ct} (base64)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(base64.b64decode(box_pub_b64)))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"amura-env").derive(shared)
    nonce = os.urandom(12)
    raw = eph.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"eph": base64.b64encode(raw).decode(), "nonce": base64.b64encode(nonce).decode(),
            "ct": base64.b64encode(AESGCM(key).encrypt(nonce, data, None)).decode()}


@bp.post("/ops/env")
def env():
    k = pinned()
    body = request.get_data()
    if not k or not k.get("box") or not verify(body, request.headers.get("X-Amura-Sig"), k):
        return jsonify(ok=False, error="ключ сервера не закреплён"), 403
    d = json.loads(body or b"{}")
    if abs(time.time() - float(d.get("t") or 0)) > 120:
        return jsonify(ok=False, error="старый запрос"), 400
    out = {n: v for n, v in os.environ.items() if n not in SKIP_ENV and not n.startswith(("RENDER", "IS_PULL_REQUEST", "GIT_"))}
    if "DATABASE_URL" in out:                      # база Render — только для копирования; на сервере своя
        out["RENDER_DATABASE_URL"] = out.pop("DATABASE_URL")
    print(f"Сервер Hoster: выданы переменные приложения, {len(out)} шт., зашифрованы ключом {k['fpr']}", flush=True)
    return jsonify(ok=True, **seal(k["box"], json.dumps(out).encode()))
