#!/usr/bin/env python3
"""Ключ сервера AMURA (Hoster.kz) и отчёты системе AMURA.

keygen — создать ключи один раз (/etc/amura/sign.key — подпись отчётов, /etc/amura/box.key — расшифровка секретов,
         которые система AMURA пришлёт зашифрованными; обе секретные части никогда не покидают сервер);
hello  — отправить публичные части и отпечаток (их закрепляют в deploy/server-key.json — только тогда отчёты принимаются);
fpr    — показать отпечаток;
send <ok|alert|info> <текст> — подписанный отчёт (alert и ok система AMURA пересылает в Telegram-уведомления);
env <файл> — получить переменные приложения (зашифрованы ключом box этого сервера) и записать файл для systemd (права 600).
"""
import base64
import hashlib
import json
import os
import socket
import sys
import time
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

DIR = os.environ.get("AMURA_KEYS", "/etc/amura")
API = os.environ.get("AMURA_API", "https://api.amura.kz")
RAW = dict(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)


def _b64(b):
    return base64.b64encode(b).decode()


def _load(name, cls):
    with open(os.path.join(DIR, name), "rb") as f:
        return cls.from_private_bytes(f.read())


def keygen():
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    for name, cls in (("sign.key", Ed25519PrivateKey), ("box.key", X25519PrivateKey)):
        p = os.path.join(DIR, name)
        if not os.path.exists(p):
            raw = cls.generate().private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)


def public():
    s = _load("sign.key", Ed25519PrivateKey).public_key().public_bytes(**RAW)
    b = _load("box.key", X25519PrivateKey).public_key().public_bytes(**RAW)
    return {"sign": _b64(s), "box": _b64(b)}


def fpr(pub=None):
    pub = pub or public()
    return hashlib.sha256((pub["sign"] + pub["box"]).encode()).hexdigest()[:16]


def _post(path, body, sign=False):
    data = json.dumps(body, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "amura-server/1.0"}
    if sign:
        headers["X-Amura-Sig"] = _b64(_load("sign.key", Ed25519PrivateKey).sign(data))
    req = urllib.request.Request(API + path, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


def hello():
    pub = public()
    return _post("/ops/hello", {**pub, "fpr": fpr(pub), "host": socket.gethostname(), "t": int(time.time())})


def send(level, text):
    return _post("/ops/report", {"level": level, "text": text[:3500], "fpr": fpr(), "host": socket.gethostname(),
                                 "t": int(time.time())}, sign=True)


def env(path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    r = _post("/ops/env", {"t": int(time.time()), "fpr": fpr()}, sign=True)
    shared = _load("box.key", X25519PrivateKey).exchange(X25519PublicKey.from_public_bytes(base64.b64decode(r["eph"])))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"amura-env").derive(shared)
    data = json.loads(AESGCM(key).decrypt(base64.b64decode(r["nonce"]), base64.b64decode(r["ct"]), None))
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for k, v in sorted(data.items()):
            if k.isidentifier() and "\n" not in str(v):
                f.write(f"{k}=" + json.dumps(str(v), ensure_ascii=False) + "\n")   # systemd EnvironmentFile: "значение"
    os.replace(tmp, path)
    return len(data)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "keygen":
        keygen()
    elif cmd == "fpr":
        print(fpr())
    elif cmd == "hello":
        print(hello())
    elif cmd == "env":
        print("переменных:", env(sys.argv[2]))
    elif cmd == "send":
        try:
            send(sys.argv[2], " ".join(sys.argv[3:]))
        except Exception as e:                      # ключ ещё не закреплён или нет связи — в журнал сервера
            print("отчёт не принят:", e, file=sys.stderr)
    else:
        sys.exit(__doc__)
