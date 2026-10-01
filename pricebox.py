"""Оптовые цены в открытом репозитории храним зашифрованными (prices.bin).
Ключ выводится из MS_TOKEN — он уже есть и в GitHub Actions, и на Render, отдельный секрет не нужен.
Шифр: HMAC-SHA256 в режиме счётчика + HMAC-тег (только стандартная библиотека)."""
import hashlib
import hmac
import json
import zlib


def _keys(token):
    base = hashlib.sha256(("amura-prices|" + token).encode()).digest()
    return hmac.new(base, b"enc", hashlib.sha256).digest(), hmac.new(base, b"mac", hashlib.sha256).digest()


def _stream(key, nonce, n):
    out, i = bytearray(), 0
    while len(out) < n:
        out += hmac.new(key, nonce + i.to_bytes(8, "big"), hashlib.sha256).digest()
        i += 1
    return bytes(out[:n])


def seal(obj, token):
    ek, mk = _keys(token)
    data = zlib.compress(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode(), 9)
    nonce = hmac.new(mk, b"iv" + data, hashlib.sha256).digest()[:16]   # одинаковые цены — одинаковый файл, без лишних коммитов
    ct = bytes(a ^ b for a, b in zip(data, _stream(ek, nonce, len(data))))
    return nonce + ct + hmac.new(mk, nonce + ct, hashlib.sha256).digest()


def unseal(blob, token):
    ek, mk = _keys(token)
    nonce, ct, tag = blob[:16], blob[16:-32], blob[-32:]
    if not hmac.compare_digest(tag, hmac.new(mk, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("prices.bin: ключ не подходит (сменился MS_TOKEN?)")
    return json.loads(zlib.decompress(bytes(a ^ b for a, b in zip(ct, _stream(ek, nonce, len(ct))))))
