"""Модуль «Финансы», раздел «Счета»: куда поступают деньги розничного сайта.

Счета компании (расчётный счёт, Kaspi, касса) и какой способ оплаты розницы на какой счёт приходит.
Оптовики платят как раньше — переводом по реквизитам из «Оплата и реквизиты»; этот раздел их не касается.
Сам банк перечисляет деньги по договору (эквайринг — на счёт из договора с банком): здесь — учёт и реквизиты,
по которым система записывает оплаты на нужный счёт и показывает реквизиты клиентам.
Защита от подмены: менять может только владелец, каждое изменение — в журнал и уведомление владельцу в Telegram.
Хранится только в нашей базе (в МойСклад не копируется).
"""
import json
import re
import time

import inbox
import order_hook as oh

KINDS = {"bank": "Расчётный счёт", "kaspi": "Kaspi", "cash": "Касса (наличные)"}
METHODS = {   # способ оплаты на розничном сайте → на какой счёт поступает
    "card": "Оплата картой на сайте (эквайринг)",
    "kaspi": "Kaspi при самовывозе",
    "cash": "Наличные при самовывозе",
}
_ready = [False]


def db():
    d = inbox.db()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        d.run(f"CREATE TABLE IF NOT EXISTS fin_account (id {pk}, kind TEXT, name TEXT, bank TEXT, iban TEXT, bic TEXT, kbe TEXT,"
              " phone TEXT, is_default INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, updated DOUBLE PRECISION)")
        d.run(f"CREATE TABLE IF NOT EXISTS fin_log (id {pk}, at DOUBLE PRECISION, who TEXT, text TEXT)")
        d.c.commit()
        _ready[0] = True
    return d


def iban_ok(iban):
    """IBAN Казахстана: KZ + 2 контрольные цифры + 16 знаков, проверка по модулю 97."""
    s = re.sub(r"\s", "", str(iban or "")).upper()
    if not re.fullmatch(r"KZ\d{2}[0-9A-Z]{16}", s):
        return False
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(num) % 97 == 1


def mask(iban):
    s = re.sub(r"\s", "", iban or "")
    return (s[:4] + " •••• " + s[-4:]) if len(s) > 8 else s


def _row(r):
    return {"id": r[0], "kind": r[1], "kindName": KINDS.get(r[1], r[1]), "name": r[2], "bank": r[3] or "", "iban": r[4] or "",
            "bic": r[5] or "", "kbe": r[6] or "", "phone": r[7] or "", "isDefault": bool(r[8])}


def accounts():
    with db() as d:
        rows = d.run("SELECT id, kind, name, bank, iban, bic, kbe, phone, is_default FROM fin_account WHERE archived=0 ORDER BY is_default DESC, id",
                     many=True) or []
    return [_row(r) for r in rows]


def routes():
    with db() as d:
        raw = inbox.get_setting(d, "fin_routes", "{}")
    try:
        r = json.loads(raw or "{}")
    except ValueError:
        r = {}
    ids = {a["id"] for a in accounts()}
    return {m: (r.get(m) if r.get(m) in ids else None) for m in METHODS}


def state():
    with db() as d:
        log = d.run("SELECT at, who, text FROM fin_log ORDER BY id DESC LIMIT 20", many=True) or []
    return {"accounts": accounts(), "routes": routes(), "methods": METHODS, "kinds": KINDS,
            "log": [{"at": a, "who": w, "text": t} for a, w, t in log]}


def _clean(a):
    kind = a.get("kind") if a.get("kind") in KINDS else "bank"
    name = str(a.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("Укажите название счёта")
    out = {"kind": kind, "name": name, "bank": str(a.get("bank") or "").strip()[:80], "iban": "", "bic": "", "kbe": "",
           "phone": "", "is_default": 1 if a.get("isDefault") else 0}
    if kind == "bank":
        iban = re.sub(r"\s", "", str(a.get("iban") or "")).upper()
        if not iban_ok(iban):
            raise ValueError("IBAN неверный: KZ и 18 знаков, например KZ12 3456 7890 1234 5678")
        bic = re.sub(r"\s", "", str(a.get("bic") or "")).upper()
        if not re.fullmatch(r"[A-Z0-9]{8}([A-Z0-9]{3})?", bic):
            raise ValueError("БИК: 8 или 11 латинских букв и цифр, например HSBKKZKX")
        kbe = re.sub(r"\D", "", str(a.get("kbe") or ""))[:2]
        out.update(iban=iban, bic=bic, kbe=kbe)
    elif kind == "kaspi":
        out["phone"] = re.sub(r"[^\d+]", "", str(a.get("phone") or ""))[:16]
    return out


def _log(d, who, text):
    d.run("INSERT INTO fin_log (at, who, text) VALUES (%s,%s,%s)", (time.time(), who, text[:500]))


def _alert(text):
    """Владельцу в Telegram — сразу, без ограничения частоты: смена реквизитов важна."""
    try:
        if oh.OWNER:
            oh.tg("sendMessage", chat_id=oh.OWNER, text="🏦 AMURA, счета: " + text)
    except Exception as e:
        print("Счета: уведомление не ушло:", e, flush=True)


def save_account(a, who):
    c = _clean(a)
    aid = a.get("id")
    with db() as d:
        if c["is_default"]:
            d.run("UPDATE fin_account SET is_default=0 WHERE kind=%s", (c["kind"],))
        if aid:
            old = d.run("SELECT name, iban, bic, phone FROM fin_account WHERE id=%s AND archived=0", (aid,), one=True)
            if not old:
                raise ValueError("Счёт не найден")
            d.run("UPDATE fin_account SET kind=%s, name=%s, bank=%s, iban=%s, bic=%s, kbe=%s, phone=%s, is_default=%s, updated=%s WHERE id=%s",
                  (c["kind"], c["name"], c["bank"], c["iban"], c["bic"], c["kbe"], c["phone"], c["is_default"], time.time(), aid))
            changed = (old[1] or "") != c["iban"] or (old[2] or "") != c["bic"] or (old[3] or "") != c["phone"]
            text = f"изменён счёт «{c['name']}»" + (f": реквизиты {mask(old[1]) or old[3] or '—'} → {mask(c['iban']) or c['phone']}" if changed else "")
        else:
            aid = d.run("INSERT INTO fin_account (kind, name, bank, iban, bic, kbe, phone, is_default, updated) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (c["kind"], c["name"], c["bank"], c["iban"], c["bic"], c["kbe"], c["phone"], c["is_default"], time.time()), ins=True)
            changed = True
            text = f"добавлен счёт «{c['name']}» {mask(c['iban']) or c['phone']}".rstrip()
        _log(d, who, text)
    if changed:
        _alert(f"{text} (кто: {who}). Если это не вы — срочно проверьте раздел «Счета».")
    return aid


def delete_account(aid, who):
    with db() as d:
        r = d.run("SELECT name, iban, phone FROM fin_account WHERE id=%s AND archived=0", (aid,), one=True)
        if not r:
            raise ValueError("Счёт не найден")
        d.run("UPDATE fin_account SET archived=1, is_default=0, updated=%s WHERE id=%s", (time.time(), aid))
        _log(d, who, f"удалён счёт «{r[0]}» {mask(r[1]) or r[2] or ''}".rstrip())
    _alert(f"удалён счёт «{r[0]}» (кто: {who})")


def save_routes(r, who):
    ids = {a["id"]: a["name"] for a in accounts()}
    clean = {m: (int(r[m]) if str(r.get(m) or "").isdigit() and int(r[m]) in ids else None) for m in METHODS}
    with db() as d:
        inbox.set_setting(d, "fin_routes", json.dumps(clean))
        _log(d, who, "куда поступают деньги: " + "; ".join(f"{METHODS[m]} → {ids.get(v, 'не выбран')}" for m, v in clean.items()))
    return clean
