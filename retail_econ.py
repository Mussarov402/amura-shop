"""Экономика розничных заказов amura.kz (владелец 20.11.2026, как «Экономика заказа» у WB/Ozon): сколько пришло, куда ушло, сколько осталось.

По заказу (пометка «Заказ с розничного сайта»):
- Пришло: товары + доставка от покупателя (оплачено; не оплачен — сумма заказа, «оценка»);
- эквайринг: Kaspi Pay 0,95 % от оплаты, картой — свой % (настройки);
- доставка: Яндекс — цена заявки (до вызова «оценка Яндекса», после «Подтвердить» — «по заявке», доставлен — «факт»);
  заявки нет / СДЭК — «оценка» = сколько заплатил покупатель;
- упаковка за заказ, налог ИП розницы %, доля подписки ApiPay (₸ в месяц / розничных заказов за месяц);
- себестоимость — как у WB/Ozon (mp_calc.product: своя → последняя приёмка → карточка МС по курсу);
- прибыль и маржа; отдельно «Доставка: покупатель заплатил N, Яндекс взял M → ±разница».
Сводка за период — Заказы → «Экономика розницы» (только владелец). Отменённые и возвраты в прибыль не входят, показываются отдельно.
Блок пишется и в описание заказа МС (при создании и когда меняется курьер) — заменяет прежний блок, остальное описание не трогает."""
import json
import re
import threading
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

import inbox
import order_hook as oh

bp = Blueprint("retail_econ", __name__)
KEY = "retail_econ"
DEFAULTS = {"acq": 0.95, "acq_card": 3.5, "tax": 2.0, "apipay": 10000, "pack": None}
LABELS = {"acq": "Эквайринг Kaspi Pay, %", "acq_card": "Эквайринг картой, %", "tax": "Налог ИП розницы, %",
          "apipay": "ApiPay, ₸ в месяц", "pack": "Упаковка, ₸ за заказ (пусто — как у WB/Ozon)"}
HEAD = "Экономика заказа, ₸"
ALMATY, MSK = timezone(timedelta(hours=5)), timezone(timedelta(hours=3))
CANCEL = re.compile(r"Отмен|Возврат", re.I)
YA_DONE = ("delivered", "delivered_finish")
YA_CALLED = re.compile(r"^(accepted|performer_|pickup_arrived|ready_for_pickup_confirmation|pickuped|delivery_arrived|pay_waiting|"
                       r"ready_for_delivery_confirmation)")


# ---------- настройки ----------
def conf():
    with inbox.db() as d:
        raw = inbox.get_setting(d, KEY, "")
    try:
        got = json.loads(raw) if raw else {}
    except ValueError:
        got = {}
    c = {**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS}}
    if c["pack"] in (None, ""):
        try:
            import mp_calc
            c["pack"], c["packFrom"] = float(mp_calc.settings().get("pack") or 0), "mp"
        except Exception:
            c["pack"] = 0.0
    return c


def save(b):
    with inbox.db() as d:
        raw = inbox.get_setting(d, KEY, "")
    try:
        cur = json.loads(raw) if raw else {}
    except ValueError:
        cur = {}
    for k in DEFAULTS:
        if k not in b:
            continue
        if k == "pack" and b[k] in (None, ""):
            cur.pop("pack", None)
            continue
        v = float(str(b[k]).replace(",", ".").replace(" ", ""))
        if v < 0 or (k in ("acq", "acq_card", "tax") and v >= 50):
            raise ValueError(f"«{LABELS[k]}»: недопустимое значение")
        cur[k] = v
    with inbox.db() as d:
        inbox.set_setting(d, KEY, json.dumps(cur, ensure_ascii=False))
    return conf()


# ---------- данные ----------
def _href_id(m):
    return ((m or {}).get("meta") or {}).get("href", "").rstrip("/").rsplit("/", 1)[-1].split("?")[0]


def _ship(desc):
    return next((l[len("Отправка: "):] for l in (desc or "").split("\n") if l.startswith("Отправка: ")), "")


def _cost(pid):
    import mp_calc
    try:
        p = mp_calc.product(pid)
        return p.get("cost"), p.get("costNote") or ""
    except Exception as e:
        return None, f"нет данных ({str(e)[:60]})"


def _claim(num):
    try:
        import courier
        with courier.db() as d:
            return courier._row(d, num)
    except Exception:
        return None


def _cdek_row(num):
    try:
        import cdek_orders
        with cdek_orders.db() as d:
            return cdek_orders.row(d, num) or {}
    except Exception:
        return {}


def month_orders(moment):
    """Розничных заказов за календарный месяц заказа (по Алматы), без отменённых — делитель подписки ApiPay. Кэш 10 минут."""
    m = (moment or "")[:7] or datetime.now(ALMATY).strftime("%Y-%m")

    def load():
        y, mo = int(m[:4]), int(m[5:7])
        a = datetime(y, mo, 1, tzinfo=ALMATY)
        b = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=ALMATY)
        rows = _orders(a, b, positions=False)
        return max(1, sum(1 for o in rows if not CANCEL.search(((o.get("state") or {}).get("name")) or "")))
    return oh.cached(f"recon_m:{m}", 600, load)


def _orders(a, b, positions=True):
    """Розничные заказы МойСклад за [a, b) (datetime с зоной) — с позициями и статусом."""
    f = lambda t: t.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")      # noqa: E731
    out = []
    for off in range(0, 2000, 100):
        j = oh.ms("GET", "/entity/customerorder", params={
            "filter": f"description~{oh.RETAIL_MARK};moment>={f(a)};moment<{f(b)}", "limit": 100, "offset": off, "order": "moment,asc",
            "expand": "state,positions" if positions else "state"}, timeout=40)
        rows = j.get("rows") or []
        out += [o for o in rows if (o.get("description") or "").startswith(oh.RETAIL_MARK)]
        if len(rows) < 100:
            break
    return out


def _positions(o):
    pos = o.get("positions") or {}
    rows = pos.get("rows") if isinstance(pos, dict) else None
    if rows is None or (isinstance(pos, dict) and (pos.get("meta") or {}).get("size", 0) > len(rows)):
        rows = oh.order_positions(o["id"])
    return rows or []


# ---------- расчёт ----------
def calc(o, c=None, claim=None, cost=_cost, per_month=None):
    """Заказ МойСклад (с позициями) → экономика. Все суммы в ₸ целыми; est — где оценка, а не факт."""
    c = c or conf()
    desc, num = o.get("description") or "", str(o.get("name") or "")
    dlv_sid = None
    try:
        dlv_sid = oh.delivery_service_id()
    except Exception:
        pass
    goods = dlv_in = 0.0
    costs, cost_notes, no_cost = 0.0, set(), []
    for p in _positions(o):
        a = p.get("assortment") or {}
        aid, kind = _href_id(a), ((a.get("meta") or {}).get("type") or "")
        s = (p.get("price") or 0) / 100 * (p.get("quantity") or 0) * (1 - (p.get("discount") or 0) / 100)
        if kind == "service":
            if aid == dlv_sid or (a.get("name") or "") == oh.DELIVERY_NAME:
                dlv_in += s
            continue
        goods += s
        v, note = cost(aid)
        if v is None:
            no_cost.append(aid)
        else:
            costs += v * (p.get("quantity") or 0)
            cost_notes.add(note.split(",")[0][:40] if note else "")
    total = (o.get("sum") or 0) / 100
    paid = (o.get("payedSum") or 0) / 100
    got = paid if paid > 0 else total
    card = oh.PAY_CARD_TXT in desc
    acq_pct = c["acq_card"] if card else c["acq"]
    ship = _ship(desc)
    claim = claim if claim is not None else (_claim(num) if "Курьер по Алматы" in ship else None)
    if claim and claim.get("price") and claim.get("status") not in ("cancelled", "cancelled_by_taxi", "failed", "estimating_failed"):
        st = claim.get("status") or ""
        dlv_out = float(claim["price"])
        dlv_note = "факт Яндекса" if st in YA_DONE else "по заявке Яндекса" if YA_CALLED.match(st) else "оценка Яндекса, курьер не вызван"
        dlv_est = st not in YA_DONE
    elif "СДЭК" in ship and _cdek_row(num).get("price"):
        r = _cdek_row(num)
        dlv_out = float(r["price"])
        dlv_est = r.get("status") not in ("DELIVERED", "POSTOMAT_RECEIVED")
        dlv_note = "факт СДЭК" if not dlv_est else "по отправлению СДЭК"
    elif "СДЭК" in ship and _cdek_row(num).get("est"):
        dlv_out, dlv_note, dlv_est = float(_cdek_row(num)["est"]), "оценка СДЭК, отправление не создано", True
    elif not ship or ship.startswith("Самовывоз"):
        dlv_out, dlv_note, dlv_est = 0.0, "самовывоз", False
    else:
        who = "СДЭК" if "СДЭК" in ship else "Яндекс"
        dlv_out, dlv_note, dlv_est = dlv_in, f"оценка: {who} — как заплатил покупатель, заявки ещё нет", True
    per_month = per_month if per_month is not None else month_orders(_almaty_day(o.get("moment", "")))
    e = {"number": num, "id": o.get("id", ""), "moment": (o.get("moment") or "")[:16], "state": ((o.get("state") or {}).get("name")) or "",
         "goods": round(goods), "dlvIn": round(dlv_in), "got": round(got), "paid": paid > 0,
         "acq": round(got * acq_pct / 100), "acqPct": acq_pct, "card": card,
         "dlvOut": round(dlv_out), "dlvNote": dlv_note, "dlvEst": dlv_est,
         "pack": round(c["pack"] or 0), "tax": round(got * c["tax"] / 100), "taxPct": c["tax"],
         "apipay": round((c["apipay"] or 0) / per_month), "perMonth": per_month,
         "cost": round(costs), "costNote": ", ".join(sorted(x for x in cost_notes if x)), "noCost": len(no_cost)}
    e["spent"] = e["acq"] + e["dlvOut"] + e["pack"] + e["tax"] + e["apipay"] + e["cost"]
    e["profit"] = e["got"] - e["spent"]
    e["margin"] = round(e["profit"] / e["got"] * 100, 1) if e["got"] else 0.0
    e["dlvDiff"] = e["dlvIn"] - e["dlvOut"]
    e["est"] = dlv_est or not e["paid"] or bool(no_cost)
    e["cancel"] = bool(CANCEL.search(e["state"]))
    return e


def _almaty_day(moment):
    try:
        return (datetime.strptime(moment[:16], "%Y-%m-%d %H:%M") + timedelta(hours=2)).strftime("%Y-%m-%d")
    except ValueError:
        return (moment or "")[:10]


def _f(v):
    return f"{round(v):,}".replace(",", " ")


def text(e):
    """Блок для описания заказа МС."""
    sign = lambda v: ("+" if v > 0 else "−" if v < 0 else "±") + _f(abs(v))      # noqa: E731
    out = [f"{HEAD}{' (есть оценки)' if e['est'] else ''}:",
           f"Пришло: товары {_f(e['goods'])} + доставка от покупателя {_f(e['dlvIn'])} = {_f(e['got'])}"
           + (" (оплачено)" if e["paid"] else " (ещё не оплачено — оценка)"),
           f"{'Эквайринг картой' if e['card'] else 'Kaspi Pay эквайринг'}: −{_f(e['acq'])} ({str(e['acqPct']).replace('.', ',')} %)",
           f"Доставка: −{_f(e['dlvOut'])} ({e['dlvNote']})",
           f"Упаковка: −{_f(e['pack'])}",
           f"Налог ИП: −{_f(e['tax'])} ({str(e['taxPct']).replace('.', ',')} %)",
           f"ApiPay подписка: −{_f(e['apipay'])} (1/{e['perMonth']} месяца)",
           f"Себестоимость: −{_f(e['cost'])}" + (f" (без закупки: {e['noCost']} поз.)" if e["noCost"] else ""),
           f"Прибыль: {_f(e['profit'])}, маржа {str(e['margin']).replace('.', ',')} %",
           f"Доставка: покупатель заплатил {_f(e['dlvIn'])}, {'СДЭК' if 'СДЭК' in e['dlvNote'] else 'Яндекс' if 'Яндекс' in e['dlvNote'] else 'доставка'} взял {_f(e['dlvOut'])} → {sign(e['dlvDiff'])}"]
    return "\n".join(out)


def for_order(o):
    """Экономика одного заказа МС (без позиций — дочитаем)."""
    return calc(o)


# ---------- описание заказа МС ----------
def with_block(desc, block):
    base = (desc or "").split("\n\n" + HEAD)[0].rstrip()
    return (base + "\n\n" + block)[:4000]


def write(order_id):
    """Пересчитать и записать блок в описание заказа МС (заменяет прежний). Ошибка — только в лог."""
    try:
        o = oh.ms("GET", f"/entity/customerorder/{order_id}", params={"expand": "state,positions"}, timeout=20)
        if not (o.get("description") or "").startswith(oh.RETAIL_MARK):
            return False
        new = with_block(o.get("description"), text(calc(o)))
        if new != (o.get("description") or ""):
            oh.ms("PUT", f"/entity/customerorder/{order_id}", json={"description": new}, timeout=20)
        return True
    except Exception as e:
        print("Экономика розницы: блок в заказ не записан:", str(e)[:200], flush=True)
        return False


def write_bg(order_id, delay=0):
    if not order_id or not oh.MS_TOKEN:                  # без ключа МойСклад (тесты, локально) — не пишем
        return
    t = threading.Timer(delay, write, args=(order_id,))
    t.daemon = True
    t.start()


# ---------- сводка ----------
def period(a, b):
    rows = _orders(a, b)
    c = conf()
    tot = {k: 0 for k in ("got", "goods", "dlvIn", "dlvOut", "acq", "pack", "tax", "apipay", "cost", "profit")}
    items, n, est, canc, canc_sum = [], 0, 0, 0, 0
    for o in rows:
        e = calc(o, c)
        items.append(e)
        if e["cancel"]:
            canc += 1
            canc_sum += (o.get("payedSum") or 0) / 100 or (o.get("sum") or 0) / 100
            continue
        n += 1
        est += e["est"]
        for k in tot:
            tot[k] += e[k]
    tot = {k: round(v) for k, v in tot.items()}
    return {"orders": n, "est": est, "cancelled": canc, "cancelledSum": round(canc_sum), **tot,
            "dlvDiff": tot["dlvIn"] - tot["dlvOut"], "avg": round(tot["got"] / n) if n else 0,
            "margin": round(tot["profit"] / tot["got"] * 100, 1) if tot["got"] else 0.0, "items": items[::-1]}


def _range(args):
    now = datetime.now(ALMATY)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    p = args.get("p") or "today"
    if p == "today":
        return day, now + timedelta(seconds=1)
    if p == "week":
        return day - timedelta(days=6), now + timedelta(seconds=1)
    if p == "month":
        return day.replace(day=1), now + timedelta(seconds=1)
    a = datetime.strptime(args.get("from", ""), "%Y-%m-%d").replace(tzinfo=ALMATY)
    b = datetime.strptime(args.get("to", ""), "%Y-%m-%d").replace(tzinfo=ALMATY) + timedelta(days=1)
    if b <= a or (b - a).days > 92:
        raise ValueError("Период — до 3 месяцев")
    return a, b


# ---------- API (только владелец: себестоимость и прибыль) ----------
def _owner():
    import admin
    if not admin.who():
        return jsonify(ok=False, error="Войдите заново"), 401
    if not admin.is_admin():
        return jsonify(ok=False, error="Экономика — только для владельца"), 403
    return None


@bp.get("/admin/api/retail/economy")
def route_period():
    err = _owner()
    if err:
        return err
    try:
        a, b = _range(request.args)
    except ValueError as e:
        return jsonify(ok=False, error=str(e) if "Период" in str(e) else "Даты — ГГГГ-ММ-ДД"), 400
    try:
        r = period(a, b)
    except Exception as e:
        return jsonify(ok=False, error="МойСклад: " + str(e)[:200]), 502
    return jsonify(ok=True, **r, conf=conf(), labels=LABELS, frm=a.strftime("%Y-%m-%d"), to=(b - timedelta(seconds=1)).strftime("%Y-%m-%d"))


@bp.get("/admin/api/orders/<number>/economy")
def route_order(number):
    err = _owner()
    if err:
        return err
    try:
        rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"name={number}", "limit": 1, "expand": "state,positions"},
                     timeout=20).get("rows") or []
    except Exception as e:
        return jsonify(ok=False, error="МойСклад: " + str(e)[:200]), 502
    if not rows or not (rows[0].get("description") or "").startswith(oh.RETAIL_MARK):
        return jsonify(ok=False, error="Это не заказ с розничного сайта"), 404
    e = calc(rows[0])
    return jsonify(ok=True, econ=e, text=text(e))


@bp.post("/admin/api/orders/<number>/economy")
def route_order_write(number):
    """Записать пересчитанный блок в описание заказа МС (кнопка «Обновить в МойСклад»)."""
    err = _owner()
    if err:
        return err
    rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"name={number}", "limit": 1}, timeout=20).get("rows") or []
    if not rows:
        return jsonify(ok=False, error="Заказ не найден"), 404
    return jsonify(ok=write(rows[0]["id"]))


@bp.route("/admin/api/retail/economy/settings", methods=["GET", "PUT"])
def route_settings():
    err = _owner()
    if err:
        return err
    if request.method == "PUT":
        try:
            return jsonify(ok=True, conf=save(request.get_json(silent=True) or {}), labels=LABELS)
        except (TypeError, ValueError) as e:
            return jsonify(ok=False, error=str(e) or "Числа — цифрами"), 400
    return jsonify(ok=True, conf=conf(), labels=LABELS)
