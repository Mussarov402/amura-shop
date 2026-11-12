"""«Настройки → Ключи площадок» (Хиро, 05.11.2026): владелец вписывает ключи Ozon / WB / ApiPay с телефона.
После переезда на Hoster переменные с Render на сервер больше не приходят, а SSH у владельца нет.

- Хранение — в нашей базе зашифрованными (тот же способ, что токены поставщиков: suppliers.seal / unseal).
- Ключ из базы важнее файла окружения сервера (app.env): при загрузке он кладётся в окружение процесса,
  код площадок читает os.environ как раньше. Поле пустое — работает значение из app.env (текущие ключи не ломаем).
- В интерфейсе и ответах API — только маска; в журнал значения не пишутся.
- Аудит: кто и когда изменил ключ (без значения) — таблица key_log.
- Только владелец. Блок подключаемый (модуль «Ключи площадок», включён по умолчанию)."""
import json
import os
import threading
import time

from flask import Blueprint, jsonify, request

import bg
import inbox
import modules

bp = Blueprint("mpkeys", __name__)
MOD = "mpkeys"
PFX = "envkey:"
GROUPS = [
    {"id": "ozon", "title": "Ozon — AMURA", "check": "ozon", "shop": "ozon",
     "keys": [("OZON_CLIENT_ID", "Client-Id", False), ("OZON_API_KEY", "Api-Key", True), ("OZON_NAME", "Название магазина", False)]},
    {"id": "ozon2", "title": "Ozon 2 — Cosmo Market", "check": "ozon", "shop": "ozon2",
     "keys": [("OZON_CLIENT_ID_2", "Client-Id", False), ("OZON_API_KEY_2", "Api-Key", True), ("OZON_NAME_2", "Название магазина", False)]},
    {"id": "wb", "title": "Wildberries", "check": "wb", "shop": "wb",
     "keys": [("WB_API_TOKEN", "Токен API", True), ("WB_NAME", "Название магазина", False)]},
    {"id": "wb2", "title": "Wildberries 2", "check": "wb", "shop": "wb2",
     "keys": [("WB_API_TOKEN_2", "Токен API", True), ("WB_NAME_2", "Название магазина", False)]},
    {"id": "freedom", "title": "Freedom Pay — оплата картой (розница)", "check": "freedom", "shop": "",
     "keys": [("FREEDOM_MERCHANT_ID", "Merchant ID", False), ("FREEDOM_SECRET_KEY", "Secret key", True),
              ("FREEDOM_TEST", "Тестовый режим (платежи без списания)", False)]},
    {"id": "apipay", "title": "Kaspi Pay (ApiPay) — розница amura.kz", "check": "apipay", "shop": "",
     "keys": [("APIPAY_API_KEY", "Ключ ApiPay", True)]},
    {"id": "apipay_opt", "title": "Kaspi Pay (ApiPay) — ИП опта", "check": "", "shop": "",
     "keys": [("APIPAY_API_KEY_OPT", "Ключ ApiPay", True)]},
]
NAMES = {k for g in GROUPS for k, _, _ in g["keys"]}
SECRET = {k for g in GROUPS for k, _, s in g["keys"] if s}
FLAGS = {"FREEDOM_TEST"}                               # переключатель: «1» — вкл, «0» — выкл
_orig = {k: os.environ.get(k) for k in NAMES}        # значения из app.env на момент запуска — вернуть, если поле очистили
_ready = [False]
_lock = threading.Lock()


def db():
    d = inbox.db()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        d.run(f"CREATE TABLE IF NOT EXISTS key_log (id {pk}, at DOUBLE PRECISION, name TEXT, action TEXT, who TEXT)")
        d.c.commit()
        _ready[0] = True
    return d


def _stored():
    """Имя → расшифрованное значение из базы (только наши имена)."""
    import suppliers
    out = {}
    with db() as d:
        rows = d.run("SELECT key, value FROM setting WHERE key LIKE %s", (PFX + "%",), many=True) or []
    for k, v in rows:
        name = k[len(PFX):]
        if name in NAMES and v:
            try:
                out[name] = suppliers.unseal(v)
            except Exception:
                print(f"Ключи площадок: {name} не расшифровался — используется значение сервера", flush=True)
    return out


def apply():
    """Ключи из базы — в окружение процесса (важнее app.env); очищенные — обратно значение app.env."""
    with _lock:
        st = _stored()
        for k in NAMES:
            if st.get(k):
                os.environ[k] = st[k]
            elif _orig.get(k) is not None:
                os.environ[k] = _orig[k]
            else:
                os.environ.pop(k, None)
    return st


def _loop():
    while True:                                          # другие процессы сервера подхватывают ключ за минуту
        time.sleep(60)
        try:
            apply()
        except Exception as e:
            print("Ключи площадок:", str(e)[:150], flush=True)


def start():
    try:
        apply()
    except Exception as e:
        print("Ключи площадок при запуске:", str(e)[:150], flush=True)
    bg.start(_loop)


def mask(v, secret=True):
    if not v:
        return ""
    if not secret:
        return v
    return "••••" + v[-4:] if len(v) > 8 else "••••"


# ---------- API (только владелец) ----------
def _owner():
    import admin
    if not modules.enabled(MOD):
        return None, (jsonify(ok=False, error="Блок «Ключи площадок» выключен"), 404)
    me = admin.who()
    if not me:
        return None, (jsonify(ok=False, error="Войдите заново"), 401)
    if me["role"] != "owner":
        return None, (jsonify(ok=False, error="Ключи — только для владельца"), 403)
    return me, None


@bp.get("/admin/api/mpkeys")
def route_list():
    _, err = _owner()
    if err:
        return err
    st = apply()
    groups = []
    for g in GROUPS:
        keys = []
        for name, label, secret in g["keys"]:
            v = os.environ.get(name, "")
            keys.append({"name": name, "label": label, "secret": secret, "set": bool(v), "value": mask(v, secret), "flag": name in FLAGS,
                         "from": "система AMURA" if st.get(name) else ("сервер" if v else "")})
        groups.append({"id": g["id"], "title": g["title"], "check": bool(g["check"]), "keys": keys})
    with db() as d:
        log = d.run("SELECT at, name, action, who FROM key_log ORDER BY id DESC LIMIT 30", many=True) or []
    return jsonify(ok=True, groups=groups, log=[{"at": a, "name": n, "action": x, "who": w} for a, n, x, w in log])


@bp.put("/admin/api/mpkeys")
def route_save():
    """{values: {ИМЯ: значение}} — непустое сохраняем, {clear: [ИМЯ]} — убираем из базы (тогда работает значение сервера)."""
    me, err = _owner()
    if err:
        return err
    import suppliers
    b = request.get_json(silent=True) or {}
    vals = {k: str(v).strip() for k, v in (b.get("values") or {}).items() if k in NAMES and str(v or "").strip()}
    clear = [k for k in (b.get("clear") or []) if k in NAMES]
    if any(vals[k] not in ("0", "1") for k in FLAGS & set(vals)):
        return jsonify(ok=False, error="Переключатель — 0 или 1"), 400
    if any(len(v) > 2000 or "\n" in v for v in vals.values()):
        return jsonify(ok=False, error="Ключ слишком длинный или с переносом строки"), 400
    who = me.get("name") or "владелец"
    with db() as d:
        for k, v in vals.items():
            inbox.set_setting(d, PFX + k, suppliers.seal(v))
            d.run("INSERT INTO key_log (at, name, action, who) VALUES (%s,%s,%s,%s)", (time.time(), k, "изменён", who))
        for k in clear:
            d.run("DELETE FROM setting WHERE key=%s", (PFX + k,))
            d.run("INSERT INTO key_log (at, name, action, who) VALUES (%s,%s,%s,%s)", (time.time(), k, "убран (снова ключ сервера)", who))
    apply()
    return jsonify(ok=True, saved=sorted(vals), cleared=clear)


@bp.post("/admin/api/mpkeys/check/<gid>")
def route_check(gid):
    """Проверка ключей площадки: Ozon — seller info (название, валюта, страна), WB — информация о продавце."""
    _, err = _owner()
    if err:
        return err
    g = next((x for x in GROUPS if x["id"] == gid and x["check"]), None)
    if not g:
        return jsonify(ok=False, error="Проверка для этого блока не нужна"), 400
    apply()
    if g["check"] == "freedom":
        return _check_freedom()
    if g["check"] == "apipay":
        return _check_apipay()
    import marketplaces as mpk
    ch = {c.key: c for c in mpk.channels()}.get(g["shop"])
    if not ch or not ch.keys_set():
        return jsonify(ok=False, error="Ключи не вписаны"), 400
    try:
        if g["check"] == "ozon":
            j = ch.res(ch.call("/v1/seller/info", {}))
            c = j.get("company") or {}
            info = {"name": c.get("name") or c.get("legal_name") or "", "currency": c.get("currency") or "", "country": c.get("country") or ""}
        else:
            j = ch.req("GET", "common", "/api/v1/seller-info")
            info = {"name": j.get("tradeMark") or j.get("name") or "", "currency": "", "country": ""}
    except mpk.ApiError as e:
        return jsonify(ok=False, error=f"Площадка не приняла ключ: {e.message}"[:300]), 502
    except Exception as e:
        return jsonify(ok=False, error=f"Не удалось проверить: {e.__class__.__name__}"), 502
    return jsonify(ok=True, **info)


def _check_freedom():
    """Freedom Pay: запрос статуса несуществующего платежа (get_status3.php) — без списания и без создания платежа.
    Неверный Merchant ID или ключ — Freedom отвечает ошибкой подписи / магазина; «платёж не найден» — ключи приняты."""
    import secrets
    import freedom
    if not freedom.keys_set():
        return jsonify(ok=False, error="Ключи не вписаны"), 400
    params = {"pg_merchant_id": freedom.merchant(), "pg_order_id": "amura-check-" + secrets.token_hex(4), "pg_salt": secrets.token_hex(8)}
    params["pg_sig"] = freedom.sign("get_status3.php", params)
    try:
        r = freedom.requests.post(f"{freedom.API}/get_status3.php", data=params, timeout=20)
        x = freedom._xml(r.text)
    except Exception as e:
        return jsonify(ok=False, error=f"Freedom не ответил: {e.__class__.__name__}"), 502
    desc = str(x.get("pg_error_description") or x.get("pg_description") or "")[:200]
    low = desc.lower()
    test = " · тестовый режим" if freedom.testing() else " · боевой режим"
    if x.get("pg_status") == "ok" or (x.get("pg_status") == "error" and any(w in low for w in ("не найден", "not found", "отсутств", "не существ"))):
        return jsonify(ok=True, name=f"Merchant {freedom.merchant()} — ключи приняты" + test, currency="", country="")
    if not x:
        return jsonify(ok=False, error=f"Freedom ответил непонятно (HTTP {r.status_code})"), 502
    return jsonify(ok=False, error=f"Freedom не принял ключи: {desc or x.get('pg_error_code') or 'ошибка'}"), 502


def _check_apipay():
    """ApiPay: запрос несуществующего счёта — ничего не создаёт. 401/403 — ключ не принят; иначе ключ работает.
    Боевой или песочница — по is_sandbox, если ApiPay его отдаёт, иначе по адресу API."""
    import apipay
    if not apipay.keys_set():
        return jsonify(ok=False, error="Ключ не вписан"), 400
    try:
        r = apipay.requests.get(apipay.API + "/invoices/0", headers={"X-API-Key": apipay.key(), "Accept": "application/json"}, timeout=20)
    except Exception as e:
        return jsonify(ok=False, error=f"ApiPay не ответил: {e.__class__.__name__}"), 502
    if r.status_code in (401, 403):
        return jsonify(ok=False, error="ApiPay не принял ключ — проверьте, что скопирован полностью"), 502
    if r.status_code >= 500:
        return jsonify(ok=False, error=f"ApiPay ответил {r.status_code} — попробуйте позже"), 502
    try:
        j = r.json()
    except ValueError:
        j = {}
    sb = j.get("is_sandbox") if isinstance(j, dict) else None
    mode = "песочница" if sb is True or (sb is None and "sandbox" in apipay.API) else "боевой" if sb is False else "режим ApiPay не сообщил"
    return jsonify(ok=True, name=f"ключ принят · {mode}", currency="", country="")
