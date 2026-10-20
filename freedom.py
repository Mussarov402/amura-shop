"""Оплата картой на розничном сайте через Freedom Pay (интернет-эквайринг, протокол Merchant API: init_payment + Result URL).

Как работает: заказ создаётся как раньше; на экране «Заказ оформлен» — кнопка «Оплатить картой». Сервер считает сумму сам
(по заказу в МойСклад), вызывает init_payment и отправляет покупателя на страницу оплаты Freedom (только карта: Visa/Mastercard,
Apple/Google Pay; pg_payment_method=bankcard — оплаты с мобильного баланса нет). Freedom сообщает результат на Result URL
(сервер → сервер, повторяет до 2 часов): проверяем подпись и сумму, отмечаем заказ оплаченным (номер платежа Freedom хранится —
нужен для споров), проводим входящий платёж к заказу в МойСклад и пишем владельцу «выбейте чек в кассе Kaspi».
Возврат — вручную в кабинете Freedom, в системе AMURA только отметка.

Ключи — только переменные окружения на Render: FREEDOM_MERCHANT_ID, FREEDOM_SECRET_KEY, FREEDOM_TEST=1 (тестовые платежи без списания).
Выключено по умолчанию: модуль «Оплата картой» (cardpay) в настройках; без ключей кнопки нет.
Подпись pg_sig: md5 строки «имя скрипта;значения параметров по алфавиту ключей;секретный ключ» (имя скрипта — последняя часть адреса)."""
import hashlib
import json
import os
import re
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests
from flask import Blueprint, Response, jsonify, request

import inbox
import modules
import order_hook as oh

bp = Blueprint("freedom", __name__)
bp.after_request(oh.cors)
API = os.environ.get("FREEDOM_API", "https://api.freedompay.kz").rstrip("/")
METHOD = os.environ.get("FREEDOM_PAYMENT_METHOD", "bankcard")      # только карта (Apple/Google Pay — тоже карта); пусто — без ограничения
RETAIL_SITE = os.environ.get("RETAIL_SITE_URL", "https://amura.kz").rstrip("/")
RESULT_SCRIPT = "result"                                             # /pay/card/result → имя скрипта в подписи
_ready = [False]
_lock = threading.Lock()


def merchant():
    return os.environ.get("FREEDOM_MERCHANT_ID", "").strip()


def secret():
    return os.environ.get("FREEDOM_SECRET_KEY", "").strip()


def testing():
    return os.environ.get("FREEDOM_TEST", "") == "1"


def keys_set():
    return bool(merchant() and secret())


def available():
    """Кнопка «Оплатить картой» видна: модуль включён владельцем и ключи вписаны на Render."""
    try:
        return keys_set() and modules.enabled("cardpay")
    except Exception:
        return False


# ---------- подпись ----------
def _flat(params):
    """Параметры по алфавиту ключей (без pg_sig); вложенные словари — тоже по алфавиту."""
    out = []
    for k in sorted(params):
        if k == "pg_sig":
            continue
        v = params[k]
        if isinstance(v, dict):
            out += _flat(v)
        elif isinstance(v, (list, tuple)):
            out += [str(x) for x in v]
        else:
            out.append("" if v is None else str(v))
    return out


def sign(script, params, key=None):
    s = ";".join([script] + _flat(params) + [key if key is not None else secret()])
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def check(script, params, key=None):
    got = str(params.get("pg_sig") or "")
    return bool(got) and secrets.compare_digest(got, sign(script, params, key))


# ---------- база ----------
def db():
    d = inbox.db()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        d.run(f"CREATE TABLE IF NOT EXISTS card_pay (id {pk}, pg_order TEXT UNIQUE, number TEXT, order_id TEXT, agent_id TEXT, amount DOUBLE PRECISION,"
              " status TEXT DEFAULT 'new', payment_id TEXT DEFAULT '', created DOUBLE PRECISION, paid_at DOUBLE PRECISION DEFAULT 0,"
              " ms_payment TEXT DEFAULT '', ms_error TEXT DEFAULT '', note TEXT DEFAULT '', test INTEGER DEFAULT 0)")
        d.run("CREATE INDEX IF NOT EXISTS card_pay_number ON card_pay (number)")
        # черновик оформления (розница, оплата картой): заказ в МойСклад создаётся только после оплаты.
        # status: new — ждёт оплаты; paid — оплачен, заказ создаётся; ordered — заказ создан; failed — оплачен, заказ не создан (повтор в фоне)
        d.run(f"CREATE TABLE IF NOT EXISTS card_draft (id {pk}, cid TEXT, data TEXT, total DOUBLE PRECISION, created DOUBLE PRECISION,"
              " status TEXT DEFAULT 'new', number TEXT DEFAULT '', order_id TEXT DEFAULT '', error TEXT DEFAULT '', tries INTEGER DEFAULT 0)")
        d.c.commit()
        _ready[0] = True
    return d


COLS = "pg_order, number, order_id, agent_id, amount, status, payment_id, created, paid_at, ms_payment, ms_error, note, test"


def _row(r):
    keys = COLS.split(", ")
    x = dict(zip(keys, r))
    return {"pgOrder": x["pg_order"], "number": x["number"], "orderId": x["order_id"], "agent": x["agent_id"], "amount": round(x["amount"] or 0),
            "status": x["status"], "paymentId": x["payment_id"] or "", "created": x["created"], "paidAt": x["paid_at"] or 0,
            "msPayment": x["ms_payment"] or "", "msError": x["ms_error"] or "", "note": x["note"] or "", "test": bool(x["test"])}


def by_pg_order(pg_order):
    with db() as d:
        r = d.run(f"SELECT {COLS} FROM card_pay WHERE pg_order=%s", (pg_order,), one=True)
    return _row(r) if r else None


def for_order(number):
    """Оплаты картой по заказу (новые сверху) — для окна заказа в системе AMURA."""
    with db() as d:
        rows = d.run(f"SELECT {COLS} FROM card_pay WHERE number=%s AND status<>'new' ORDER BY created DESC", (str(number),), many=True) or []
    return [_row(r) for r in rows]


def paid_numbers(numbers):
    """Какие из заказов уже оплачены картой (в т.ч. тестом — такие в МойСклад не пишутся, а кнопка «Оплатить» не нужна)."""
    numbers = [str(n) for n in numbers if n]
    if not numbers:
        return set()
    with db() as d:
        rows = d.run("SELECT DISTINCT number FROM card_pay WHERE status='paid' AND number IN (" + ",".join(["%s"] * len(numbers)) + ")",
                     tuple(numbers), many=True) or []
    return {r[0] for r in rows}


def _set(pg_order, **f):
    with db() as d:
        d.run(f"UPDATE card_pay SET {', '.join(k + '=%s' for k in f)} WHERE pg_order=%s", (*f.values(), pg_order))


# ---------- создание платежа ----------
def _order(number):
    rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"name={number}", "limit": 1}, timeout=15).get("rows") or []
    return rows[0] if rows else None


# ---------- черновик: заказ появляется в МойСклад только после оплаты ----------
DRAFT_TTL = 86400                 # неоплаченный черновик живёт сутки («Ожидает оплаты» в «Моих заказах»)
DCOLS = "id, cid, data, total, created, status, number, order_id, error, tries"
_draft_lock = threading.Lock()


def _drow(r):
    x = dict(zip(DCOLS.split(", "), r))
    x["data"] = json.loads(x["data"] or "{}")
    x["total"] = round(x["total"] or 0)
    return x


def save_draft(cid, data):
    with db() as d:
        return d.run("INSERT INTO card_draft (cid, data, total, created, status) VALUES (%s, %s, %s, %s, 'new')",
                     (cid or "", json.dumps(data, ensure_ascii=False), data["total"], time.time()), ins=True)


def get_draft(did):
    with db() as d:
        r = d.run(f"SELECT {DCOLS} FROM card_draft WHERE id=%s", (int(did),), one=True)
    return _drow(r) if r else None


def _dset(did, **f):
    with db() as d:
        d.run("UPDATE card_draft SET " + ", ".join(f"{k}=%s" for k in f) + " WHERE id=%s", (*f.values(), int(did)))


def draft_token(did):
    return f"D{did}_{oh.sign('D' + str(did))}"


def my_drafts(cid):
    """«Мои заказы»: неоплаченные черновики за сутки — «Ожидает оплаты» с кнопкой «Оплатить»."""
    if not cid:
        return []
    with db() as d:
        rows = d.run(f"SELECT {DCOLS} FROM card_draft WHERE cid=%s AND status='new' AND created>%s ORDER BY created DESC LIMIT 10",
                     (cid, time.time() - DRAFT_TTL), many=True) or []
    return [_drow(r) for r in rows]


def _pay_target(number):
    """(сумма, id заказа МойСклад, id покупателя) — по черновику «D<id>» или по заказу МойСклад."""
    if str(number).startswith("D"):
        dr = get_draft(int(str(number)[1:]))
        if not dr:
            raise ValueError("Заказ не найден")
        if dr["status"] != "new":
            raise ValueError("Заказ уже оплачен")
        if time.time() - dr["created"] > DRAFT_TTL:
            raise ValueError("Время на оплату истекло — оформите заказ заново")
        return dr["total"], "", dr["cid"]
    o = _order(number)
    if not o:
        raise ValueError("Заказ не найден")
    if not (o.get("description") or "").startswith(oh.RETAIL_MARK):
        raise ValueError("Оплата картой — только для заказов с розничного сайта")
    if (o.get("payedSum") or 0) >= (o.get("sum") or 0) > 0:
        raise ValueError("Заказ уже оплачен")
    agent = (((o.get("agent") or {}).get("meta") or {}).get("href") or "").rsplit("/", 1)[-1]
    return round((o.get("sum") or 0) / 100), o.get("id") or "", agent


def init(number):
    """Платёж по заказу или черновику: сумма — из МойСклад / черновика (браузеру не доверяем). Возвращает ссылку на страницу оплаты Freedom."""
    amount, order_id, agent = _pay_target(number)
    if amount <= 0:
        raise ValueError("Сумма заказа не определена")
    if str(number).startswith("D"):                                   # ушёл платить картой — живой счёт Kaspi по черновику отменяем
        try:
            import apipay
            apipay.cancel_active(str(number))
        except Exception as e:
            print("Freedom Pay: счёт Kaspi не отменён:", str(e)[:200], flush=True)
    with db() as d:
        if d.run("SELECT 1 FROM card_pay WHERE number=%s AND status='paid'", (str(number),), one=True):
            raise ValueError("Заказ уже оплачен")
        n = (d.run("SELECT COUNT(*) FROM card_pay WHERE number=%s", (str(number),), one=True) or [0])[0]
    pg_order = f"{number}-{n + 1}"
    back = draft_token(str(number)[1:]) if str(number).startswith("D") else number   # черновик: сайт по подписи узнаёт номер заказа после оплаты                                 # каждая попытка — свой номер у Freedom, заказ один
    params = {"pg_merchant_id": merchant(), "pg_order_id": pg_order, "pg_amount": str(amount), "pg_currency": "KZT",
              "pg_description": "Заказ в AMURA" if str(number).startswith("D") else f"Заказ №{number} в AMURA", "pg_salt": secrets.token_hex(8), "pg_language": "ru",
              "pg_result_url": f"{oh.PUBLIC_URL}/pay/card/{RESULT_SCRIPT}", "pg_request_method": "POST",
              "pg_success_url": f"{RETAIL_SITE}/?pay=ok&n={back}#me", "pg_failure_url": f"{RETAIL_SITE}/?pay=fail&n={back}#me",
              "pg_success_url_method": "GET", "pg_failure_url_method": "GET"}
    if METHOD:
        params["pg_payment_method"] = METHOD
    if testing():
        params["pg_testing_mode"] = "1"
    params["pg_sig"] = sign("init_payment.php", params)
    with db() as d:
        d.run(f"INSERT INTO card_pay ({COLS}) VALUES (%s, %s, %s, %s, %s, 'new', '', %s, 0, '', '', '', %s)",
              (pg_order, str(number), order_id, agent, amount, time.time(), 1 if testing() else 0))
    r = requests.post(f"{API}/init_payment.php", data=params, timeout=20)
    x = _xml(r.text)
    if x.get("pg_status") != "ok" or not x.get("pg_redirect_url"):
        _set(pg_order, status="error", note=(x.get("pg_error_description") or x.get("pg_description") or r.text[:200])[:300])
        print("Freedom Pay: платёж не создан:", x.get("pg_error_code"), (x.get("pg_error_description") or "")[:200], flush=True)
        raise ValueError("Оплата картой сейчас недоступна, попробуйте позже — кнопка «Оплатить» есть в «Моих заказах»")
    _set(pg_order, payment_id=x.get("pg_payment_id") or "")
    return x["pg_redirect_url"]


def _xml(text):
    try:
        root = ET.fromstring(text)
        return {c.tag: (c.text or "") for c in root}
    except ET.ParseError:
        return {}


def _answer(status, description):
    """Ответ Freedom на Result URL — XML с подписью тем же скриптом."""
    p = {"pg_status": status, "pg_description": description, "pg_salt": secrets.token_hex(8)}
    p["pg_sig"] = sign(RESULT_SCRIPT, p)
    body = "<?xml version=\"1.0\" encoding=\"utf-8\"?><response>" + "".join(f"<{k}>{escape(v)}</{k}>" for k, v in p.items()) + "</response>"
    return Response(body, mimetype="application/xml")


# ---------- уведомление об оплате ----------
def handle_result(params):
    """Result URL: подпись, сумма, идемпотентность. Возвращает (pg_status, описание)."""
    if not keys_set():
        return "rejected", "not configured"
    if not check(RESULT_SCRIPT, params):
        print("Freedom Pay: неверная подпись уведомления", params.get("pg_order_id"), flush=True)
        return "rejected", "bad signature"
    if str(params.get("pg_merchant_id") or merchant()) != merchant():
        return "rejected", "wrong merchant"
    pg_order = str(params.get("pg_order_id") or "")
    with _lock:
        p = by_pg_order(pg_order)
        if not p:
            return "rejected", "unknown order"
        pay_id = str(params.get("pg_payment_id") or "")
        if p["status"] == "paid":                                    # повтор того же уведомления — уже учтено
            return "ok", "already paid"
        if str(params.get("pg_result")) != "1":
            _set(pg_order, status="failed", payment_id=pay_id or p["paymentId"], note=str(params.get("pg_failure_description") or "")[:300])
            return "ok", "failure noted"
        try:
            amount = float(params.get("pg_amount") or 0)
        except (TypeError, ValueError):
            amount = 0
        if abs(amount - p["amount"]) > 0.5 or str(params.get("pg_currency") or "KZT") != "KZT":
            _set(pg_order, status="mismatch", payment_id=pay_id, note=f"Сумма {amount} вместо {p['amount']}")
            oh.alert("cardpay", f"оплата картой по заказу № {p['number']}: сумма {amount:.0f} ₸ вместо {p['amount']} ₸ — проверьте в кабинете Freedom")
            return "rejected", "amount mismatch"
        _set(pg_order, status="paid", payment_id=pay_id, paid_at=time.time())
        if p["number"].startswith("D"):                              # черновик оплачен — «Ожидает оплаты» больше не показываем
            _dset(int(p["number"][1:]), status="paid")
    oh.notify_bg(lambda: after_paid(pg_order))
    return "ok", "accepted"


@bp.route(f"/pay/card/{RESULT_SCRIPT}", methods=["GET", "POST"])
def result_url():
    params = request.form.to_dict() if request.method == "POST" else request.args.to_dict()
    status, descr = handle_result(params)
    if status == "rejected" and str(params.get("pg_can_reject")) != "1" and descr == "amount mismatch":
        status = "ok"                                                # отказ невозможен — платёж уже прошёл, разберёмся вручную
    return _answer(status, descr)


@bp.route("/pay/card/status", methods=["POST", "OPTIONS"])
def status_route():
    """Сайт после оплаты: заказ по черновику создаётся за секунды — номер подтягиваем отсюда."""
    if request.method == "OPTIONS":
        return "", 204
    number, _, tok = str((request.get_json(silent=True) or {}).get("token") or "").partition("_")
    if not number.startswith("D") or not number[1:].isdigit() or not secrets.compare_digest(tok, oh.sign(number)):
        return jsonify(ok=False, error="Заказ не найден"), 404
    dr = get_draft(int(number[1:]))
    if not dr:
        return jsonify(ok=False, error="Заказ не найден"), 404
    data = dr["data"]                                                # для экрана «Заказ оплачен»: состав, сумма, получение
    return jsonify(ok=True, status=dr["status"], number=dr["number"], total=dr["total"], ship=data.get("shipName", ""), dlv=data.get("dlv", 0),
                   items=[{"id": l["id"], "name": l["name"], "qty": l["qty"], "price": l["price"]} for l in data.get("lines") or []])


@bp.route("/pay/card/init", methods=["POST", "OPTIONS"])
def init_route():
    """Сайт: «Оплатить картой» после оформления. Доступ — по подписи заказа (startToken из ответа на заказ)."""
    if request.method == "OPTIONS":
        return "", 204
    if not available():
        return jsonify(ok=False, error="Оплата картой пока недоступна"), 404
    b = request.get_json(silent=True) or {}
    number, _, tok = str(b.get("token") or "").partition("_")
    if not number or not re.fullmatch(r"[\w-]{1,20}", number) or not secrets.compare_digest(tok, oh.sign(number)):
        return jsonify(ok=False, error="Заказ не найден"), 404
    if oh.too_many("cardpay:" + number, limit=10, window=3600):
        return jsonify(ok=False, error="Слишком много попыток, подождите"), 429
    try:
        return jsonify(ok=True, url=init(number))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        print("Freedom Pay: ошибка создания платежа:", str(e)[:200], flush=True)
        return jsonify(ok=False, error="Оплата картой сейчас недоступна, попробуйте позже"), 502


# ---------- после оплаты: МойСклад и владелец ----------
def _ms_account(method="card"):
    """Счёт организации в МойСклад для оплат картой (method="card") или Kaspi ("kaspi"): тот, что выбран в «Счетах» (по номеру счёта);
    не найден — счёт по умолчанию."""
    try:
        import finance
        aid = finance.routes().get(method)
        acc = next((a for a in finance.accounts() if a["id"] == aid), None) if aid else None
        iban = re.sub(r"\s", "", (acc or {}).get("iban") or "")
        if not iban:
            return None
        org = oh.organization()
        rows = oh.ms("GET", f"/entity/organization/{org}/accounts", timeout=20)
        rows = rows if isinstance(rows, list) else rows.get("rows", [])
        hit = next((r for r in rows if re.sub(r"\s", "", r.get("accountNumber") or "") == iban), None)
        return {"meta": hit["meta"]} if hit else None
    except Exception as e:
        print("Freedom Pay: счёт для платежа не определён:", str(e)[:200], flush=True)
        return None


def post_ms(pg_order):
    """Входящий платёж к заказу в МойСклад. externalCode — номер платежа Freedom: повтор не создаст второй платёж."""
    p = by_pg_order(pg_order)
    if not p or p["status"] != "paid" or p["msPayment"]:
        return p
    ext = "freedom-" + (p["paymentId"] or pg_order)
    try:
        found = oh.ms("GET", "/entity/paymentin", params={"filter": f"externalCode={ext}", "limit": 1}, timeout=20).get("rows") or []
        row = found[0] if found else None
        if row is None:
            body = {"externalCode": ext, "organization": oh.meta("organization", oh.organization()), "agent": oh.meta("counterparty", p["agent"]),
                    "applicable": True, "sum": p["amount"] * 100,
                    "paymentPurpose": f"Оплата картой Freedom Pay{' (ТЕСТ)' if p['test'] else ''}, платёж №{p['paymentId']}, заказ №{p['number']}"[:255],
                    "description": f"Из системы AMURA: оплата картой на сайте, Freedom Pay №{p['paymentId']}"}
            if p["orderId"]:
                body["operations"] = [{**oh.meta("customerorder", p["orderId"]), "linkedSum": p["amount"] * 100}]
            acc = _ms_account()
            if acc:
                body["organizationAccount"] = acc
            row = oh.ms("POST", "/entity/paymentin", json=body, timeout=30)
        _set(pg_order, ms_payment=row.get("name") or row.get("id") or "", ms_error="")
    except Exception as e:
        _set(pg_order, ms_error=str(e)[:300])
        print("Freedom Pay: платёж в МойСклад не записан:", str(e)[:200], flush=True)
    return by_pg_order(pg_order)


PAID_STATE = "Подтвержден"      # статус заказа в МойСклад после оплаты картой: подтверждён, к сборке («Собран» ставит склад)


def to_assembly(p, how="картой", who="Freedom Pay", test_note=""):
    """Оплачен — заказ «Новый» → «Подтвержден» (передан в сборку). Уже дальше по пути — не трогаем. Тест — тоже, с пометкой «ТЕСТ» в комментарии.
    how / who / test_note — для Kaspi (apipay.py): «Оплачен Kaspi», «ApiPay»."""
    if not p["orderId"]:
        return False
    md = oh.cached("ms_order_states", 3600, lambda: oh.ms("GET", "/entity/customerorder/metadata", timeout=15))
    states = {s["meta"]["href"].rsplit("/", 1)[-1]: s for s in md.get("states", [])}
    target = next((s for s in states.values() if s.get("name") == PAID_STATE), None)
    o = oh.ms("GET", f"/entity/customerorder/{p['orderId']}", timeout=15)
    cur = states.get(((o.get("state") or {}).get("meta") or {}).get("href", "").rsplit("/", 1)[-1], {}).get("name", "Новый")
    if not target or cur != "Новый":
        return False
    body = {"state": {"meta": target["meta"]}}
    if p["test"]:
        body["description"] = ((o.get("description") or "") + "\n" + (test_note or f"ТЕСТ: оплата картой Freedom Pay без списания, платёж №{p['paymentId']}"))[:4000]
    oh.ms("PUT", f"/entity/customerorder/{p['orderId']}", json=body, timeout=20)
    try:
        import admin
        admin._olog(p["number"], f"Оплачен {how}{' (ТЕСТ)' if p['test'] else ''} → статус «{PAID_STATE}»", who_name=who)
    except Exception:
        pass
    oh._cache.pop("adm_orders", None)
    return True


def order_draft(pg_order):
    """Оплачен черновик — создаём заказ в МойСклад (сразу «Подтвержден»). Повтор — не создаёт второй заказ.
    Не вышло (нет товара, МойСклад не ответил) — деньги не теряем: тревога владельцу один раз, повтор в фоне. Возвращает (ok, причина)."""
    p = by_pg_order(pg_order)
    did = int(p["number"][1:])
    with _draft_lock:
        dr = get_draft(did)
        if not dr:
            return False, "черновик не найден"
        if dr["status"] == "ordered":
            _set(pg_order, number=dr["number"], order_id=dr["order_id"])
            return True, ""
        data = dr["data"]
        md = oh.cached("ms_order_states", 3600, lambda: oh.ms("GET", "/entity/customerorder/metadata", timeout=15))
        st = next((x for x in md.get("states", []) if x.get("name") == PAID_STATE), None)
        note = f"Оплачено картой Freedom Pay, платёж №{p['paymentId']}" + (" — ТЕСТ, без списания" if p["test"] else "")
        try:
            res, code = oh.order_core(data["d"], f"draft-{did}", "", dr["cid"],
                                      paid={k: data[k] for k in ("lines", "dlv", "shipName", "total")} | {"note": note, "state": st and st["meta"]})
        except Exception as e:
            res, code = {"error": str(e)[:300]}, 500
        if code == 200:
            _dset(did, status="ordered", number=str(res["number"]), order_id=res.get("orderId", ""), error="")
            _set(pg_order, number=str(res["number"]), order_id=res.get("orderId", ""))
            return True, ""
        why = res.get("error") or "ошибка"
        _dset(did, status="failed", error=why[:300], tries=dr["tries"] + 1)
    if dr["tries"] == 0:                                           # тревога — один раз, дальше молча пробуем в фоне
        oh.alert("cardpay", f"оплата картой получена ({oh.fmt(p['amount'])} ₸{', ТЕСТ' if p['test'] else ''}, Freedom № {p['paymentId']}), "
                            f"но заказ не создан: {why}. Пробую ещё раз каждые 10 минут; возврат — в кабинете Freedom")
    return False, why


def after_paid(pg_order):
    p = by_pg_order(pg_order)
    if not p:
        return
    if p["number"].startswith("D"):                                 # оплачен черновик — теперь создаём заказ
        ok, why = order_draft(pg_order)
        if not ok:
            return
        p = by_pg_order(pg_order)
    if not p["test"]:                                                # тестовые платежи в МойСклад не пишем
        p = post_ms(pg_order) or p
    try:
        to_assembly(p)
    except Exception as e:
        print("Freedom Pay: статус заказа не сменён:", str(e)[:200], flush=True)
    text = (f"💳 Оплачен заказ № {p['number']} картой, {oh.fmt(p['amount'])} ₸{' (ТЕСТ, без списания)' if p['test'] else ''}\n"
            f"Freedom Pay № {p['paymentId']}\nЗаказ передан в сборку. Выбейте чек в кассе Kaspi."
            + (f"\n⚠️ В МойСклад платёж не записан: {p['msError']}" if p["msError"] else ""))
    try:
        oh.tg("sendMessage", chat_id=oh.OWNER, text=text)
    except Exception as e:
        print("Freedom Pay: уведомление владельцу не ушло:", str(e)[:200], flush=True)
    try:
        import push
        push.notify(f"💳 Оплачен заказ № {p['number']}", f"{oh.fmt(p['amount'])} ₸ картой — выбейте чек в кассе Kaspi", "/admin#orders", perm="orders", tag=f"pay{p['number']}")
    except Exception as e:
        print("Push об оплате:", e, flush=True)


def retry_drafts():
    """Оплаченные черновики, по которым заказ не создался, — ещё раз (до 3 суток)."""
    with db() as d:
        rows = d.run("SELECT p.pg_order FROM card_draft c JOIN card_pay p ON p.number='D' || c.id WHERE c.status='failed' "
                     "AND p.status='paid' AND c.created>%s", (time.time() - 3 * 86400,), many=True) or []
    for (pg,) in rows:
        try:
            after_paid(pg)
        except Exception as e:
            print("Freedom Pay: повтор заказа по оплате:", str(e)[:200], flush=True)


def retry_ms():
    """Оплаченные, но не записанные в МойСклад (МойСклад не ответил) — ещё раз. Вызывается из окна заказа и фоном."""
    with db() as d:
        rows = d.run("SELECT pg_order FROM card_pay WHERE status='paid' AND ms_payment='' AND test=0 AND paid_at>%s",
                     (time.time() - 7 * 86400,), many=True) or []
    for (pg,) in rows:
        post_ms(pg)


def mark_refund(pg_order, who=""):
    """Возврат делается вручную в кабинете Freedom — здесь только отметка."""
    p = by_pg_order(pg_order)
    if not p or p["status"] != "paid":
        raise ValueError("Оплата не найдена")
    _set(pg_order, status="refund", note=(f"Возврат отмечен: {who}" if who else "Возврат отмечен")[:300])
    return by_pg_order(pg_order)


def _loop():
    while True:
        time.sleep(600)
        try:
            if keys_set():
                retry_ms()
                retry_drafts()
        except Exception as e:
            print("Freedom Pay: повтор записи в МойСклад:", str(e)[:200], flush=True)


def start():
    threading.Thread(target=_loop, daemon=True).start()
