"""Покупатель сам отменяет ОПЛАЧЕННЫЙ заказ в «Моих заказах» (задача Хиро 19.11.2026). Деньги возвращает только владелец.

- Отменить можно, пока заказ не ушёл: статус «Новый» / «Подтвержден», нет отгрузки (ни в МойСклад, ни нашей ship_op),
  нет заявки курьеру Яндекса. Иначе — «Заказ уже передан в доставку — оформите возврат» (через поддержку).
- Отмена: в МойСклад статус «Отменен», резерв снят, в комментарий — «Отменён покупателем на сайте ДД.ММ ЧЧ:ММ, ждёт возврата N ₸»;
  владельцу — Telegram «верните N ₸ через Kaspi Pay». Автоматических возвратов нет.
- В системе AMURA (окно заказа) владелец жмёт «Деньги возвращены» → исходящий платёж в МойСклад со счёта Kaspi (статья «Возврат»,
  externalCode refund-<номер> — повтор не создаст второй), счета Kaspi / картой по заказу отмечаются «возврат».
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
        _ready[0] = True
    return d


COLS = "number, order_id, agent_id, amount, created, done_at, done_by, ms_payment, ms_error"


def _row(r):
    x = dict(zip(COLS.split(", "), r))
    return {"number": x["number"], "orderId": x["order_id"] or "", "agent": x["agent_id"] or "", "amount": round(x["amount"] or 0),
            "created": x["created"] or 0, "done": bool(x["done_at"]), "doneAt": x["done_at"] or 0, "doneBy": x["done_by"] or "",
            "msPayment": x["ms_payment"] or "", "msError": x["ms_error"] or ""}


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
            d.run(f"INSERT INTO client_cancel ({COLS}) VALUES (%s,%s,%s,%s,%s,0,'','','')", (number, o["id"], cid, amount, time.time()))
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
