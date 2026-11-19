"""Покупатель сам отменяет ОПЛАЧЕННЫЙ заказ в «Моих заказах» (задача Хиро 19.11.2026). Деньги возвращает только владелец.

- Отменить можно, пока заказ не ушёл: статус «Новый» / «Подтвержден», нет отгрузки (ни в МойСклад, ни нашей ship_op),
  нет заявки курьеру Яндекса. Иначе — «Заказ уже передан в доставку — оформите возврат» (через поддержку).
- Отмена: в МойСклад статус «Отменен», резерв снят, в комментарий — «Отменён покупателем на сайте ДД.ММ ЧЧ:ММ, ждёт возврата N ₸»;
  владельцу — Telegram «верните N ₸ через Kaspi Pay». Автоматических возвратов нет.
- В системе AMURA (окно заказа) владелец жмёт «Деньги возвращены» → исходящий платёж в МойСклад со счёта Kaspi (статья «Возврат»,
  externalCode refund-<номер> — повтор не создаст второй), счета Kaspi / картой по заказу отмечаются «возврат».
- «Вернуть через Kaspi» (задача Хиро 19.11.2026): владелец жмёт кнопку → POST /invoices/{id}/refund в ApiPay (полный возврат,
  ключ идемпотентности по номеру заказа). Возврат асинхронный: итог берём опросом GET /invoices/{id}/refunds (фоновая сверка ApiPay
  раз в минуту и при открытии заказа). Деньги вернулись → то же, что «Деньги возвращены». Отказ → понятная причина, ничего не отмечаем,
  можно нажать ещё раз (новый ключ). «Исход не подтверждён» (uncertain) → повторять нельзя, разбор в кабинете ApiPay.
- Покупатель видит «Отменён — возврат N ₸ в обработке», затем «Отменён — деньги возвращены»."""
import threading
import time
from datetime import datetime

import inbox
import order_hook as oh

CANCELLABLE = ("Новый", "Подтвержден")
CANCEL_STATE = "Отменен"
SHIPPED = "Заказ уже передан в доставку — оформите возврат"
_ready = [False]
_lock = threading.Lock()


def db():
    d = inbox.db()
    if not _ready[0]:
        d.run("CREATE TABLE IF NOT EXISTS client_cancel (number TEXT PRIMARY KEY, order_id TEXT, agent_id TEXT, amount DOUBLE PRECISION,"
              " created DOUBLE PRECISION, done_at DOUBLE PRECISION DEFAULT 0, done_by TEXT DEFAULT '', ms_payment TEXT DEFAULT '', ms_error TEXT DEFAULT '')")
        d.c.commit()
        for col in ("kr_id INTEGER DEFAULT 0", "kr_status TEXT DEFAULT ''", "kr_key TEXT DEFAULT ''", "kr_error TEXT DEFAULT ''",
                    "kr_try INTEGER DEFAULT 0", "kr_who TEXT DEFAULT ''", "kr_invoice INTEGER DEFAULT 0"):
            try:                                                # колонки возврата через Kaspi (ApiPay) — к таблице из #570
                with inbox.db() as d2:
                    d2.run(f"ALTER TABLE client_cancel ADD COLUMN {col}")
            except Exception:
                pass
        d.c.commit()
        _ready[0] = True
    return d


COLS = "number, order_id, agent_id, amount, created, done_at, done_by, ms_payment, ms_error, kr_id, kr_status, kr_key, kr_error, kr_try, kr_who, kr_invoice"


def _row(r):
    x = dict(zip(COLS.split(", "), r))
    return {"number": x["number"], "orderId": x["order_id"] or "", "agent": x["agent_id"] or "", "amount": round(x["amount"] or 0),
            "created": x["created"] or 0, "done": bool(x["done_at"]), "doneAt": x["done_at"] or 0, "doneBy": x["done_by"] or "",
            "msPayment": x["ms_payment"] or "", "msError": x["ms_error"] or "",
            "kaspiRefund": {"id": x["kr_id"] or 0, "status": x["kr_status"] or "", "error": x["kr_error"] or "", "tries": x["kr_try"] or 0,
                            "key": x["kr_key"] or "", "who": x["kr_who"] or "", "invoice": x["kr_invoice"] or 0}}


def get(number):
    with db() as d:
        r = d.run(f"SELECT {COLS} FROM client_cancel WHERE number=%s", (str(number),), one=True)
    return _row(r) if r else None


def for_agent(cid):
    with db() as d:
        rows = d.run(f"SELECT {COLS} FROM client_cancel WHERE agent_id=%s", (str(cid),), many=True) or []
    return {r[0]: _row(r) for r in rows}


def _set(number, **f):
    with db() as d:
        d.run(f"UPDATE client_cancel SET {', '.join(k + '=%s' for k in f)} WHERE number=%s", (*f.values(), str(number)))


def _state_name(o):
    st = o.get("state") or {}
    if st.get("name"):
        return st["name"]
    href = (st.get("meta") or {}).get("href", "").rstrip("/").rsplit("/", 1)[-1]
    md = oh.cached("ms_order_states", 3600, lambda: oh.ms("GET", "/entity/customerorder/metadata", timeout=15))
    return next((s["name"] for s in md.get("states", []) if s["meta"]["href"].rstrip("/").endswith("/" + href)), "Новый") if href else "Новый"


def _shipped(o, tracks):
    """Заказ уже в пути: отгрузка в МойСклад, наша отгрузка (ship_op) или подтверждённая заявка курьеру."""
    if o.get("demands") or o["name"] in tracks:
        return True
    try:
        import shipments
        return bool(shipments.for_order(o["id"]))
    except Exception:
        return False


def can_cancel(o, tracks=()):
    """Для «Моих заказов»: оплачен, статус «Новый»/«Подтвержден», ничего не отгружено."""
    paid = (o.get("payedSum") or 0) > 0
    return paid and _state_name(o) in CANCELLABLE and not _shipped(o, tracks)


def cancel(cid, number):
    """Покупатель нажал «Да, отменить». ValueError — понятная причина для покупателя."""
    with _lock:
        if get(number):
            raise ValueError("Заказ уже отменён")
        rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"name={number}", "limit": 1, "expand": "state"}, timeout=30).get("rows") or []
        o = rows[0] if rows else None
        if not o or (o.get("agent") or {}).get("meta", {}).get("href", "").rstrip("/").rsplit("/", 1)[-1] != cid:
            raise LookupError("Заказ не найден")
        try:
            import courier
            tracks = courier.client_tracks(cid, {number})
        except Exception:
            tracks = {}
        if _shipped(o, tracks) or _state_name(o) not in CANCELLABLE:
            raise ValueError(SHIPPED)
        amount = round((o.get("payedSum") or 0) / 100)
        if amount <= 0:
            raise ValueError("Заказ не оплачен — напишите в поддержку")
        md = oh.cached("ms_order_states", 3600, lambda: oh.ms("GET", "/entity/customerorder/metadata", timeout=15))
        st = next((s for s in md.get("states", []) if s.get("name") == CANCEL_STATE), None)
        if not st:
            raise RuntimeError("В МойСклад нет статуса «Отменен»")
        positions = []
        for p in oh.order_positions(o["id"]):
            if p.get("reserve"):
                positions.append({"id": p["id"], "quantity": p["quantity"], "price": p["price"],
                                  "assortment": {"meta": p["assortment"]["meta"]}, "reserve": 0})
        when = datetime.now(oh.ALMATY).strftime("%d.%m %H:%M")
        body = {"state": {"meta": st["meta"]},
                "description": ((o.get("description") or "") + f"\nОтменён покупателем на сайте {when}, ждёт возврата {oh.fmt(amount)} ₸")[:4000]}
        if positions:
            body["positions"] = positions
        oh.ms("PUT", f"/entity/customerorder/{o['id']}", json=body, timeout=55)
        with db() as d:
            d.run("INSERT INTO client_cancel (number, order_id, agent_id, amount, created) VALUES (%s,%s,%s,%s,%s)", (number, o["id"], cid, amount, time.time()))
    try:
        import admin
        admin._olog(number, f"Отменён покупателем на сайте, ждёт возврата {oh.fmt(amount)} ₸ → статус «{CANCEL_STATE}», резерв снят", who_name="Покупатель")
    except Exception:
        pass
    oh._cache.pop("adm_orders", None)
    try:
        oh.tg("sendMessage", chat_id=oh.OWNER, text=f"↩️ Покупатель отменил оплаченный заказ № {number}, верните {oh.fmt(amount)} ₸ через Kaspi Pay.\n"
                                                     "Вернули — нажмите «Деньги возвращены» в окне заказа в системе AMURA.")
    except Exception as e:
        print("Отмена покупателем: уведомление владельцу не ушло:", str(e)[:200], flush=True)
    return get(number)


def _expense_item():
    rows = oh.cached("ms_exp_refund", 86400, lambda: oh.ms("GET", "/entity/expenseitem", params={"filter": "name=Возврат", "limit": 1}, timeout=20))
    rows = rows.get("rows") or []
    return {"meta": rows[0]["meta"]} if rows else None


def refund_done(number, who=""):
    """Владелец вернул деньги в Kaspi Pay: исходящий платёж в МойСклад (один), отметки «возврат» у счетов по заказу."""
    c = get(number)
    if not c:
        raise ValueError("Этот заказ покупатель не отменял")
    if c["done"] and c["msPayment"]:
        return c
    ext = f"refund-{number}"
    err = ""
    try:
        found = oh.ms("GET", "/entity/paymentout", params={"filter": f"externalCode={ext}", "limit": 1}, timeout=20).get("rows") or []
        row = found[0] if found else None
        if row is None:
            body = {"externalCode": ext, "organization": oh.meta("organization", oh.organization()), "agent": oh.meta("counterparty", c["agent"]),
                    "applicable": True, "sum": c["amount"] * 100,
                    "paymentPurpose": f"Возврат за отменённый заказ №{number} (Kaspi Pay)"[:255],
                    "description": "Из системы AMURA: покупатель отменил оплаченный заказ на сайте, деньги возвращены"}
            exp = _expense_item()
            if exp:
                body["expenseItem"] = exp
            import freedom
            acc = freedom._ms_account("kaspi")
            if acc:
                body["organizationAccount"] = acc
            try:
                row = oh.ms("POST", "/entity/paymentout", json={**body, "operations": [{**oh.meta("customerorder", c["orderId"]), "linkedSum": c["amount"] * 100}]}, timeout=30)
            except Exception as e:                                    # МойСклад не дал привязать к заказу — платёж всё равно записываем
                print("Возврат: без привязки к заказу:", str(e)[:200], flush=True)
                row = oh.ms("POST", "/entity/paymentout", json=body, timeout=30)
        _set(number, done_at=time.time(), done_by=who[:80], ms_payment=row.get("name") or row.get("id") or "", ms_error="")
    except Exception as e:
        err = str(e)[:300]
        _set(number, done_at=time.time(), done_by=who[:80], ms_error=err)
        print("Возврат: платёж в МойСклад не записан:", err[:200], flush=True)
    for mod, rows_of, key in (("apipay", "for_order", "invoiceId"), ("freedom", "for_order", "pgOrder")):
        try:
            m = __import__(mod)
            for p in getattr(m, rows_of)(number):
                if p["status"] == "paid":
                    m.mark_refund(p[key], who)
        except Exception as e:
            print(f"Возврат: отметка {mod}:", str(e)[:200], flush=True)
    try:
        import admin
        admin._olog(number, f"Деньги возвращены покупателю: {oh.fmt(c['amount'])} ₸" + (f" (в МойСклад не записано: {err})" if err else ""), who_name=who or None)
    except Exception:
        pass
    return get(number)


# ---------- «Вернуть через Kaspi» (ApiPay) ----------
LIVE = ("pending", "processing")
REASONS = {
    "refund_insufficient_funds": "На счёте Kaspi Pay не хватает денег на полный возврат — пополните счёт и нажмите ещё раз",
    "refund_window_expired": "Срок возврата через Kaspi истёк (около 14 дней с оплаты) — верните деньги переводом и нажмите «Деньги возвращены»",
    "refund_rejected_by_kaspi": "Kaspi отклонил возврат — нужна ссылка-возврат для покупателя (кабинет ApiPay → Возвраты) или возврат в приложении Kaspi Pay",
    "refund_requires_buyer_confirmation": "Kaspi требует подтверждения покупателя — выпустите ссылку-возврат в кабинете ApiPay или верните в Kaspi Pay",
    "kaspi_session_expired": "Сессия кассира Kaspi истекла — войдите заново в кабинете ApiPay и нажмите ещё раз",
    "kaspi_session_invalid": "Сессия кассира Kaspi недействительна — войдите заново в кабинете ApiPay и нажмите ещё раз",
    "kaspi_session_unavailable": "Kaspi сейчас недоступен для кассира — попробуйте через несколько минут",
    "tariff_inactive": "Не оплачен тариф ApiPay — возвраты через API закрыты. Верните в Kaspi Pay и нажмите «Деньги возвращены»",
    "qr_refund_link_invoice_busy": "По этому счёту уже идёт возврат — дождитесь итога",
    "invoice_not_refundable": "Этот счёт Kaspi вернуть нельзя — верните в Kaspi Pay и нажмите «Деньги возвращены»",
    "refund_amount_exceeds_available": "По счёту уже был возврат — проверьте его в кабинете ApiPay",
    "kaspi_throttled": "Kaspi просит подождать — нажмите ещё раз через минуту",
    "network_unavailable": "Нет связи с Kaspi — нажмите ещё раз через минуту",
    "refund_resolved_not_executed": "Возврат не прошёл (разобран в кабинете ApiPay) — можно нажать ещё раз",
}


def _reason(code, msg=""):
    if code in REASONS:
        return REASONS[code]
    if "недостаточно" in (msg or "").lower():
        return REASONS["refund_insufficient_funds"]
    return "ApiPay: " + (msg or code or "возврат не прошёл")[:200]


def kaspi_invoice(number):
    """Оплаченный счёт Kaspi по заказу (для кнопки «Вернуть через Kaspi»)."""
    try:
        import apipay
        return next((p for p in apipay.for_order(number) if p["status"] == "paid"), None)
    except Exception:
        return None


def _finish(number, who):
    return refund_done(number, who)


def kaspi_refund(number, who=""):
    """Кнопка владельца. Возвращает запись; ValueError — причина по-русски (ничего не отмечено)."""
    import apipay
    with _lock:
        c = get(number)
        if not c:
            raise ValueError("Этот заказ покупатель не отменял")
        if c["done"]:
            raise ValueError("Деньги по этому заказу уже возвращены")
        kr = c["kaspiRefund"]
        if kr["status"] in LIVE or kr["status"] == "uncertain":
            pass                                              # возврат уже в Kaspi — второй не создаём, только сверяем
        else:
            p = kaspi_invoice(number)
            if not p:
                raise ValueError("Оплаты Kaspi по этому заказу нет — верните вручную и нажмите «Деньги возвращены»")
            if not apipay.keys_set():
                raise ValueError("Ключ ApiPay не вписан — верните в Kaspi Pay и нажмите «Деньги возвращены»")
            tries = kr["tries"] + 1
            key = f"amura-refund-{number}-{tries}"            # один ключ на попытку; после отказа — новый (как требует ApiPay)
            _set(number, kr_key=key, kr_try=tries, kr_invoice=p["invoiceId"], kr_who=who[:80], kr_status="sending", kr_error="")
            try:
                j = apipay.call("POST", f"/invoices/{p['invoiceId']}/refund",
                                json={"idempotency_key": key, "reason": f"Покупатель отменил заказ №{number} на сайте"[:255]})
            except apipay.ApiError as e:
                why = _reason(e.error_code, e.message)
                _set(number, kr_status="failed", kr_error=why[:300])
                raise ValueError(why)
            except Exception as e:                            # ответ потерялся: тот же ключ повторит тот же возврат
                _set(number, kr_status="pending", kr_error="")
                print("Возврат Kaspi: ответ не получен:", str(e)[:200], flush=True)
                return get(number)
            r = j.get("refund") or {}
            _set(number, kr_id=int(r.get("id") or 0), kr_status=r.get("status") or "pending")
    return poll(number)


def poll(number):
    """Сверка возврата с ApiPay: деньги вернулись → «Деньги возвращены»; отказ → причина; не ясно → «не повторять»."""
    import apipay
    c = get(number)
    if not c or c["done"]:
        return c
    kr = c["kaspiRefund"]
    if kr["status"] not in LIVE + ("uncertain", "sending") or not kr["invoice"]:
        return c
    try:
        j = apipay.call("GET", f"/invoices/{kr['invoice']}/refunds")
    except Exception as e:
        print("Возврат Kaspi: сверка:", str(e)[:200], flush=True)
        return c
    rows = j.get("refunds") or []
    r = next((x for x in rows if (kr["id"] and x.get("id") == kr["id"]) or (kr["key"] and x.get("idempotency_key") == kr["key"])), None)
    if not r:
        if kr["status"] == "sending":                         # заявка так и не дошла до ApiPay — можно нажать ещё раз
            _set(number, kr_status="failed", kr_error="Заявка на возврат не дошла до ApiPay — нажмите ещё раз")
        return get(number)
    state = r.get("operation_state") or r.get("status") or ""
    if r.get("id") and not kr["id"]:
        _set(number, kr_id=int(r["id"]))
    if state == "completed" or r.get("error_code") == "refund_resolved_executed":
        _set(number, kr_status="completed", kr_error="")
        return _finish(number, (kr["who"] or "Владелец") + " — через Kaspi (ApiPay)")
    if state in ("failed", "awaiting_buyer", "released"):
        _set(number, kr_status="failed", kr_error=_reason(r.get("error_code") or "", r.get("error_message") or "")[:300])
    elif state == "uncertain" or r.get("error_code") == "refund_execution_uncertain":
        _set(number, kr_status="uncertain", kr_error="Kaspi не подтвердил исход — деньги могли уйти. Не возвращайте повторно: "
                                                     "сверьте в кабинете ApiPay (карточка счёта) и историей Kaspi Pay")
    else:
        _set(number, kr_status="processing")
    return get(number)


def poll_pending():
    """Фон (сверка ApiPay раз в минуту): все незавершённые возвраты через Kaspi."""
    with db() as d:
        rows = d.run("SELECT number FROM client_cancel WHERE done_at=0 AND kr_status IN ('pending', 'processing', 'sending', 'uncertain')", many=True) or []
    for (n,) in rows:
        try:
            poll(n)
        except Exception as e:
            print("Возврат Kaspi: фоновая сверка:", str(e)[:200], flush=True)
