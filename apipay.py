"""Оплата Kaspi на розничном сайте через ApiPay.kz (независимый сервис поверх Kaspi Pay владельца, не официальная интеграция Kaspi).

Как работает: покупатель выбирает «Kaspi» и нажимает «Заказать и оплатить» — у нас сохраняется черновик (как у оплаты картой,
freedom.save_draft), в МойСклад пока ничего не пишем. Сервер сам считает сумму (товары + доставка, только целые тенге) и выставляет
счёт на номер телефона из заказа (POST /invoices, одной суммой amount, без корзины). Покупателю приходит push в приложении Kaspi,
счёт живёт 24 часа. Об оплате узнаём опросом GET /invoices/{id} (webhook пока не делаем): пока покупатель на экране — раз в 3–5 с,
дальше фоновая сверка раз в минуту всех неоплаченных; закрытые (отменён / истёк / ошибка) — ещё 24 часа, они могут стать оплаченными.
Оплачен — как у Freedom (freedom.after_paid): заказ из черновика в МойСклад сразу «Подтвержден», входящий платёж на счёт Kaspi
(«Счета» → способ «kaspi»), владельцу сообщение в Telegram. Счёт песочницы (is_sandbox) — тест: платёж в МойСклад не пишем.

Ключ — только переменная окружения на Render: APIPAY_API_KEY (заголовок X-API-Key). Адрес — APIPAY_API (по умолчанию боевой).
Выключено по умолчанию: модуль «Оплата Kaspi» (kaspipay) в настройках; без ключа варианта «Kaspi» на сайте нет.
Справочник: https://apipay.kz/openapi.json (поля сверены 20.10.2026, версия 2.1.0)."""
import bg
import os
import re
import secrets
import threading
import time

import requests
from flask import Blueprint, jsonify, request

import freedom
import inbox
import modules
import order_hook as oh

bp = Blueprint("apipay", __name__)
bp.after_request(oh.cors)
API = os.environ.get("APIPAY_API", "https://api.apipay.kz/api/v1").rstrip("/")
ACTIVE = ("processing", "pending", "cancelling")        # счёт ещё может быть оплачен — опрашиваем
CLOSED = ("cancelled", "expired", "error")              # закрыт, но Kaspi может прислать «оплачен» — смотрим ещё сутки
WATCH_CLOSED = 86400
POLL_GAP = 3                                            # экран покупателя: не чаще раза в 3 с на счёт
TTL = 86400                                             # счёт по номеру живёт сутки
_ready = [False]
_lock = threading.Lock()
_seen = {}                                              # invoice_id → когда последний раз спрашивали ApiPay
RETRY_GAP = 600                                         # повтор «оплачен, но заказ / платёж не создан» — не чаще раза в 10 минут
_retry_at = [0.0]


def key():
    return os.environ.get("APIPAY_API_KEY", "").strip()


def keys_set():
    return bool(key())


def available():
    """Вариант «Kaspi» на сайте: модуль включён владельцем и ключ вписан на Render."""
    try:
        return keys_set() and modules.enabled("kaspipay")
    except Exception:
        return False


def phone8(phone):
    """Номер из заказа → 8XXXXXXXXXX, как требует ApiPay. Не казахстанский мобильный (городской 71x/72x и т. п.) — пусто."""
    s = re.sub(r"\D", "", str(phone or ""))
    if len(s) == 11 and s[0] in "78":
        s = s[1:]
    return "8" + s if re.fullmatch(r"7[04-8]\d{8}", s) else ""


def shown(p8):
    return f"+7 {p8[1:4]} {p8[4:7]} {p8[7:9]} {p8[9:11]}" if len(p8) == 11 else p8


# ---------- запросы к ApiPay ----------
class ApiError(Exception):
    def __init__(self, code, error_code, message):
        super().__init__(message)
        self.code, self.error_code, self.message = code, error_code, message


def call(method, path, **kw):
    r = requests.request(method, API + path, headers={"X-API-Key": key(), "Accept": "application/json"}, timeout=20, **kw)
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code >= 400:
        msg = j.get("message") or ""
        if j.get("errors"):
            msg += " " + "; ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in j["errors"].items())
        raise ApiError(r.status_code, str(j.get("error_code") or j.get("error") or ""), (msg.strip() or f"HTTP {r.status_code}")[:300])
    return j


# ---------- база ----------
COLS = "invoice_id, number, order_id, agent_id, amount, phone, status, created, paid_at, closed_at, ms_payment, ms_error, note, test"


def db():
    if not _ready[0]:
        with freedom.db():                                # таблица черновиков (card_draft) — для сверки и after_paid
            pass
    d = inbox.db()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        # status — как у ApiPay (processing / pending / paid / cancelled / expired / error …) + наши: mismatch (сумма не совпала), refund (возврат отмечен)
        d.run(f"CREATE TABLE IF NOT EXISTS kaspi_pay (id {pk}, invoice_id INTEGER UNIQUE, number TEXT, order_id TEXT DEFAULT '', agent_id TEXT DEFAULT '',"
              " amount DOUBLE PRECISION, phone TEXT, status TEXT, created DOUBLE PRECISION, paid_at DOUBLE PRECISION DEFAULT 0,"
              " closed_at DOUBLE PRECISION DEFAULT 0, ms_payment TEXT DEFAULT '', ms_error TEXT DEFAULT '', note TEXT DEFAULT '', test INTEGER DEFAULT 0)")
        d.run("CREATE INDEX IF NOT EXISTS kaspi_pay_number ON kaspi_pay (number)")
        d.c.commit()
        _ready[0] = True
    return d


def _row(r):
    x = dict(zip(COLS.split(", "), r))
    return {"invoiceId": x["invoice_id"], "paymentId": str(x["invoice_id"]), "number": x["number"], "orderId": x["order_id"] or "",
            "agent": x["agent_id"] or "", "amount": round(x["amount"] or 0), "phone": x["phone"] or "", "status": x["status"],
            "created": x["created"], "paidAt": x["paid_at"] or 0, "closedAt": x["closed_at"] or 0, "msPayment": x["ms_payment"] or "",
            "msError": x["ms_error"] or "", "note": x["note"] or "", "test": bool(x["test"])}


def by_invoice(iid):
    with db() as d:
        r = d.run(f"SELECT {COLS} FROM kaspi_pay WHERE invoice_id=%s", (int(iid),), one=True)
    return _row(r) if r else None


def last_for(number):
    with db() as d:
        r = d.run(f"SELECT {COLS} FROM kaspi_pay WHERE number=%s ORDER BY created DESC LIMIT 1", (str(number),), one=True)
    return _row(r) if r else None


def for_order(number):
    """Счета Kaspi по заказу (новые сверху) — для окна заказа в системе AMURA."""
    with db() as d:
        rows = d.run(f"SELECT {COLS} FROM kaspi_pay WHERE number=%s ORDER BY created DESC", (str(number),), many=True) or []
    return [_row(r) for r in rows]


def _set(iid, **f):
    with db() as d:
        d.run(f"UPDATE kaspi_pay SET {', '.join(k + '=%s' for k in f)} WHERE invoice_id=%s", (*f.values(), int(iid)))


# ---------- выставление счёта ----------
def _draft(number):
    if not re.fullmatch(r"D\d+", str(number)):
        raise ValueError("Оплата Kaspi — только при оформлении заказа на сайте")
    dr = freedom.get_draft(int(number[1:]))
    if not dr:
        raise ValueError("Заказ не найден")
    return dr


def init(number):
    """Счёт по черновику «D<id>»: сумма — из черновика (браузеру не доверяем), номер — из заказа. Уже есть живой счёт — он и возвращается."""
    dr = _draft(number)
    if dr["status"] != "new":
        raise ValueError("Заказ уже оплачен")
    if time.time() - dr["created"] > freedom.DRAFT_TTL:
        raise ValueError("Время на оплату истекло — оформите заказ заново")
    amount = int(dr["total"])
    if amount < 1 or amount != dr["total"]:
        raise ValueError("Сумма заказа не определена")
    p8 = phone8((dr["data"].get("d") or {}).get("phone"))
    if not p8:
        raise ValueError("Счёт Kaspi выставляется на казахстанский мобильный номер — укажите его в заказе или оплатите картой")
    with _lock:
        cur = last_for(number)
        if cur and cur["status"] in ACTIVE:
            return cur
        if cur and cur["status"] == "paid":
            raise ValueError("Заказ уже оплачен")
        body = {"phone_number": p8, "amount": amount, "description": "Заказ AMURA Cosmetics на сайте amura.kz",
                "external_order_id": number, "internal_comment": f"Розница amura.kz, черновик {number}",
                # один ключ на черновик: ApiPay не даст второй живой счёт, а после отмены / истечения выставит новый
                "external_order_id_idempotency": f"amura-{number}-{int(dr['created'])}"}
        try:
            j = call("POST", "/invoices", json=body)
        except ApiError as e:
            print("ApiPay: счёт не выставлен:", e.code, e.error_code, e.message[:200], flush=True)
            if e.error_code in ("kaspi_session_expired", "kaspi_session_invalid", "tariff_inactive", "kyc_rejected", "kyc_daily_limit_reached"):
                oh.alert("kaspipay", f"оплата Kaspi на сайте не работает: ApiPay ответил «{e.error_code}» — {e.message[:200]}. Проверьте кабинет ApiPay")
            raise ValueError("Kaspi сейчас не принимает счёт, попробуйте позже или оплатите картой") from e
        iid = int(j["id"])
        with db() as d:
            d.run(f"INSERT INTO kaspi_pay ({COLS}) VALUES (%s, %s, '', %s, %s, %s, %s, %s, 0, 0, '', '', '', 0)",
                  (iid, number, dr["cid"] or "", amount, p8, str(j.get("status") or "processing"), time.time()))
    refresh(iid, force=True)
    return by_invoice(iid)


def cancel_active(number):
    """Покупатель ушёл платить картой — живой счёт Kaspi по этому черновику отменяем, чтобы не заплатить дважды."""
    p = last_for(number)
    if not p or p["status"] not in ("processing", "pending"):
        return
    try:
        call("POST", f"/invoices/{p['invoiceId']}/cancel")
        refresh(p["invoiceId"], force=True)
    except Exception as e:
        print("ApiPay: счёт не отменён:", str(e)[:200], flush=True)


# ---------- статус: опрос ApiPay ----------
def refresh(iid, force=False):
    """Спрашиваем ApiPay о счёте и записываем статус. Стал «оплачен» — проверяем сумму и запускаем after_paid (один раз)."""
    p = by_invoice(iid)
    if not p or p["status"] in ("paid", "mismatch", "refund"):
        return p
    now = time.time()
    if not force and now - _seen.get(iid, 0) < POLL_GAP:
        return p
    _seen[iid] = now
    try:
        j = call("GET", f"/invoices/{iid}")
    except Exception as e:
        print("ApiPay: статус счёта не получен:", iid, str(e)[:200], flush=True)
        return p
    st = str(j.get("status") or p["status"])
    test = 1 if j.get("is_sandbox") else 0
    run = False
    with _lock:
        p = by_invoice(iid)
        if p["status"] in ("paid", "mismatch", "refund"):
            return p
        if st == "paid":
            try:
                amount = float(j.get("amount") or 0)
            except (TypeError, ValueError):
                amount = 0
            if abs(amount - p["amount"]) > 0.5:
                _set(iid, status="mismatch", test=test, note=f"Сумма {amount:.0f} вместо {p['amount']}")
                oh.alert("kaspipay", f"оплата Kaspi по заказу {p['number']}: сумма {amount:.0f} ₸ вместо {p['amount']} ₸ — проверьте в кабинете ApiPay")
                return by_invoice(iid)
            _set(iid, status="paid", test=test, paid_at=now, note="")
            dr = freedom.get_draft(int(p["number"][1:])) if p["number"].startswith("D") else None
            if dr and dr["status"] == "new":
                freedom._dset(dr["id"], status="paid")
                run = True
            elif dr:                                             # черновик уже оплачен картой — деньги пришли дважды
                _set(iid, note="Заказ уже был оплачен — верните деньги в Kaspi Pay")
                oh.alert("kaspipay", f"двойная оплата: счёт Kaspi № {iid} ({oh.fmt(p['amount'])} ₸) оплачен, а заказ уже оплачен раньше. "
                                     "Верните деньги покупателю в Kaspi Pay")
        else:
            note = str(j.get("error_message") or "")[:300] if st == "error" else p["note"]
            closed = p["closedAt"] or (now if st in CLOSED else 0)
            _set(iid, status=st, test=test, closed_at=closed, note=note)
    if run:
        oh.notify_bg(lambda: after_paid(iid))
    return by_invoice(iid)


# ---------- после оплаты: заказ, МойСклад, владелец ----------
def order_draft(iid):
    """Как freedom.order_draft: оплаченный черновик → заказ в МойСклад сразу «Подтвержден». Повтор не создаёт второй заказ."""
    p = by_invoice(iid)
    did = int(p["number"][1:])
    with freedom._draft_lock:
        dr = freedom.get_draft(did)
        if not dr:
            return False, "черновик не найден"
        if dr["status"] == "ordered":
            _set(iid, number=dr["number"], order_id=dr["order_id"])
            return True, ""
        data = dr["data"]
        md = oh.cached("ms_order_states", 3600, lambda: oh.ms("GET", "/entity/customerorder/metadata", timeout=15))
        st = next((x for x in md.get("states", []) if x.get("name") == freedom.PAID_STATE), None)
        note = f"Оплачено Kaspi (ApiPay), счёт №{iid}" + (" — ТЕСТ, песочница ApiPay, без денег" if p["test"] else "")
        try:
            res, code = oh.order_core(data["d"], f"draft-{did}", "", dr["cid"],
                                      paid={k: data[k] for k in ("lines", "dlv", "shipName", "total")} | {"note": note, "state": st and st["meta"]})
        except Exception as e:
            res, code = {"error": str(e)[:300]}, 500
        if code == 200:
            freedom._dset(did, status="ordered", number=str(res["number"]), order_id=res.get("orderId", ""), error="")
            _set(iid, number=str(res["number"]), order_id=res.get("orderId", ""))
            return True, ""
        why = res.get("error") or "ошибка"
        freedom._dset(did, status="failed", error=why[:300], tries=dr["tries"] + 1)
    if dr["tries"] == 0:
        oh.alert("kaspipay", f"оплата Kaspi получена ({oh.fmt(p['amount'])} ₸{', ТЕСТ' if p['test'] else ''}, счёт ApiPay № {iid}), "
                             f"но заказ не создан: {why}. Пробую ещё раз каждые 10 минут; возврат — в Kaspi Pay")
    return False, why


def post_ms(iid):
    """Входящий платёж к заказу в МойСклад на счёт Kaspi. externalCode — номер счёта ApiPay: повтор не создаст второй платёж."""
    p = by_invoice(iid)
    if not p or p["status"] != "paid" or p["msPayment"] or p["test"]:
        return p
    ext = f"apipay-{iid}"
    try:
        found = oh.ms("GET", "/entity/paymentin", params={"filter": f"externalCode={ext}", "limit": 1}, timeout=20).get("rows") or []
        row = found[0] if found else None
        if row is None:
            body = {"externalCode": ext, "organization": oh.meta("organization", oh.organization()), "agent": oh.meta("counterparty", p["agent"]),
                    "applicable": True, "sum": p["amount"] * 100,
                    "paymentPurpose": f"Оплачено Kaspi (ApiPay), счёт №{iid}, заказ №{p['number']}"[:255],
                    "description": f"Из системы AMURA: оплата Kaspi на сайте, счёт ApiPay №{iid}"}
            if p["orderId"]:
                body["operations"] = [{**oh.meta("customerorder", p["orderId"]), "linkedSum": p["amount"] * 100}]
            acc = freedom._ms_account("kaspi")
            if acc:
                body["organizationAccount"] = acc
            row = oh.ms("POST", "/entity/paymentin", json=body, timeout=30)
        _set(iid, ms_payment=row.get("name") or row.get("id") or "", ms_error="")
    except Exception as e:
        _set(iid, ms_error=str(e)[:300])
        print("ApiPay: платёж в МойСклад не записан:", str(e)[:200], flush=True)
    return by_invoice(iid)


def after_paid(iid):
    p = by_invoice(iid)
    if not p or p["status"] != "paid":
        return
    if p["number"].startswith("D"):
        ok, why = order_draft(iid)
        if not ok:
            return
        p = by_invoice(iid)
    if not p["test"]:                                                # тестовые (песочница) в МойСклад не пишем
        p = post_ms(iid) or p
    try:
        freedom.to_assembly(p, how="Kaspi", who="ApiPay", test_note=f"ТЕСТ: оплата Kaspi в песочнице ApiPay, без денег, счёт №{iid}")
    except Exception as e:
        print("ApiPay: статус заказа не сменён:", str(e)[:200], flush=True)
    text = (f"🟥 Оплачен заказ № {p['number']} через Kaspi, {oh.fmt(p['amount'])} ₸{' (ТЕСТ, песочница ApiPay)' if p['test'] else ''}\n"
            f"Счёт ApiPay № {iid}, номер {shown(p['phone'])}\nЗаказ передан в сборку."
            + (f"\n⚠️ В МойСклад платёж не записан: {p['msError']}" if p["msError"] else ""))
    try:
        oh.tg("sendMessage", chat_id=oh.OWNER, text=text)
    except Exception as e:
        print("ApiPay: уведомление владельцу не ушло:", str(e)[:200], flush=True)
    try:
        import push
        push.notify(f"🟥 Оплачен заказ № {p['number']}", f"{oh.fmt(p['amount'])} ₸ через Kaspi", "/admin#orders", perm="orders", tag=f"pay{p['number']}")
    except Exception as e:
        print("Push об оплате:", e, flush=True)


def mark_refund(iid, who=""):
    """Возврат — в Kaspi Pay / кабинете ApiPay вручную; здесь только отметка."""
    p = by_invoice(iid)
    if not p or p["status"] != "paid":
        raise ValueError("Оплата не найдена")
    _set(iid, status="refund", note=(f"Возврат отмечен: {who}" if who else "Возврат отмечен")[:300])
    return by_invoice(iid)


# ---------- адреса для сайта ----------
def _token():
    number, _, tok = str((request.get_json(silent=True) or {}).get("token") or "").partition("_")
    if not re.fullmatch(r"D\d{1,12}", number) or not secrets.compare_digest(tok, oh.sign(number)):
        return None
    return number


def _view(p, number):
    """Что видит покупатель: статус счёта, номер телефона; оплачен — номер заказа (появляется через секунды)."""
    out = {"ok": True, "status": p["status"] if p else "none", "phone": shown(p["phone"]) if p else "", "total": p["amount"] if p else 0}
    if p and p["status"] == "error":
        out["error"] = "Kaspi не принял счёт" + (f": {p['note']}" if p["note"] else "")
    dr = freedom.get_draft(int(number[1:]))
    if dr:
        out.update(draft=dr["status"], number=dr["number"], total=dr["total"])
    return out


@bp.route("/pay/kaspi/init", methods=["POST", "OPTIONS"])
def init_route():
    """Сайт: «Оплатить через Kaspi» — счёт на номер из заказа. Доступ — по подписи черновика (payToken из ответа на заказ)."""
    if request.method == "OPTIONS":
        return "", 204
    if not available():
        return jsonify(ok=False, error="Оплата Kaspi пока недоступна"), 404
    number = _token()
    if not number:
        return jsonify(ok=False, error="Заказ не найден"), 404
    if oh.too_many("kaspipay:" + number, limit=10, window=3600):
        return jsonify(ok=False, error="Слишком много попыток, подождите"), 429
    try:
        return jsonify(_view(init(number), number))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        print("ApiPay: ошибка выставления счёта:", str(e)[:200], flush=True)
        return jsonify(ok=False, error="Kaspi сейчас не принимает счёт, попробуйте позже или оплатите картой"), 502


@bp.route("/pay/kaspi/status", methods=["POST", "OPTIONS"])
def status_route():
    """Экран «Счёт отправлен в Kaspi»: сайт спрашивает раз в 3–5 с, сервер — ApiPay не чаще раза в 3 с на счёт."""
    if request.method == "OPTIONS":
        return "", 204
    number = _token()
    if not number:
        return jsonify(ok=False, error="Заказ не найден"), 404
    p = last_for(number)
    if p and keys_set():
        p = refresh(p["invoiceId"]) or p
    return jsonify(_view(p, number))


# ---------- фоновая сверка ----------
def sweep():
    """Все неоплаченные счета за 2 суток + закрытые за последние 24 ч (могут стать оплаченными); оплаченные без заказа / без МойСклад — ещё раз."""
    now = time.time()
    with db() as d:
        rows = d.run("SELECT invoice_id FROM kaspi_pay WHERE created>%s AND (status IN ('processing', 'pending', 'cancelling') "
                     "OR (status IN ('cancelled', 'expired', 'error') AND closed_at>%s))", (now - 2 * TTL, now - WATCH_CLOSED), many=True) or []
        # заказ не создался (failed) или создание прервалось перезапуском сервера (paid дольше 2 минут)
        again = d.run("SELECT p.invoice_id FROM kaspi_pay p JOIN card_draft c ON p.number='D' || c.id WHERE p.status='paid' "
                      "AND (c.status='failed' OR (c.status='paid' AND p.paid_at<%s)) AND c.created>%s", (now - 120, now - 3 * 86400), many=True) or []
        nopay = d.run("SELECT invoice_id FROM kaspi_pay WHERE status='paid' AND ms_payment='' AND test=0 AND number NOT LIKE 'D%%' AND paid_at>%s",
                      (now - 7 * 86400,), many=True) or []
    for (iid,) in rows:
        refresh(iid, force=True)
    if now - _retry_at[0] < RETRY_GAP:                 # счета опрашиваем раз в минуту, а заказ в МойСклад повторяем раз в 10 минут
        return
    _retry_at[0] = now
    for (iid,) in again + nopay:
        try:
            after_paid(iid) if (iid,) in again else post_ms(iid)
        except Exception as e:
            print("ApiPay: повтор после оплаты:", str(e)[:200], flush=True)


def _loop():
    while True:
        time.sleep(60)
        try:
            if keys_set():
                sweep()
        except Exception as e:
            print("ApiPay: фоновая сверка:", str(e)[:200], flush=True)


def start():
    bg.start(_loop)
