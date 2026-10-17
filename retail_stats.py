"""«Сегодня»: выручка розничного сайта (amura.kz) как в кабинете WB — Заказано / Выкуплено / Отменено, процент выкупа.
Данные — из нашей копии МойСклад (ms_doc), только заказы с пометкой «Заказ с розничного сайта». В МойСклад ничего не пишем.

Как считаем:
- Заказано — оплаченные заказы (оплачено ≥ суммы) по дате создания;
- Выкуплено — статус «Доставлен», у самовывоза и «Отгружен» (забрал сам); дата — последнее изменение заказа
  (смена статуса на «Доставлен»/«Отгружен» — последнее, что с ним делают; отдельной даты выкупа в МойСклад нет);
- Отменено / возвраты — статусы «Отменен» и «Возврат», по дате последнего изменения;
- процент выкупа — выкуплено / (выкуплено + отменено), как у WB.
Время МойСклад — Москва (UTC+3), дни — по Алматы (UTC+5)."""
import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

import admin
import mirror
import order_hook as oh

bp = Blueprint("retail_stats", __name__)
ALMATY, MSK = timezone(timedelta(hours=5)), timezone(timedelta(hours=3))
PERIODS = ("today", "yesterday", "7", "30")


def bounds(period, now=None):
    """[начало, конец) периода — строки времени МойСклад (Москва), дни по Алматы."""
    now = (now or datetime.now(ALMATY)).astimezone(ALMATY)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    a, b = {"today": (day, now + timedelta(seconds=1)), "yesterday": (day - timedelta(days=1), day),
            "7": (day - timedelta(days=6), now + timedelta(seconds=1)), "30": (day - timedelta(days=29), now + timedelta(seconds=1))}[period]
    f = lambda t: t.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")
    return f(a), f(b)


def _states():
    md = admin.fresh("adm_order_states", 600, admin._ms_states)
    return {s["meta"]["href"].rsplit("/", 1)[-1]: s.get("name", "") for s in md.get("states", [])}


def _pickup(order_id):
    """Самовывоз? В копии МойСклад только первая строка комментария — способ получения читаем из заказа один раз и запоминаем."""
    key = ("rpick", order_id)
    if key not in oh._cache:
        try:
            o = oh.ms("GET", f"/entity/customerorder/{order_id}", timeout=10)
            oh._cache[key] = "Отправка: Самовывоз" in (o.get("description") or "")
        except Exception:
            return False
    return oh._cache[key]


def stats(period, now=None, states=None, pickup=_pickup):
    a, b = bounds(period, now)
    states = states if states is not None else _states()
    with mirror.db() as d:
        rows = d.run("SELECT id, moment, updated, state_id, sum, COALESCE(paid, 0) FROM ms_doc WHERE type='customerorder' AND deleted=0 "
                     "AND descr=%s AND (moment>=%s OR updated>=%s)", (oh.RETAIL_MARK, a, a), many=True) or []
    t = {k: [0, 0] for k in ("ordered", "bought", "cancelled")}
    for oid, moment, updated, sid, total, paid in rows:
        total, st = round(total or 0), states.get(sid or "", "Новый")
        if a <= (moment or "") < b and total > 0 and paid >= total:
            t["ordered"][0] += 1
            t["ordered"][1] += total
        if not (a <= (updated or "") < b):
            continue
        if re.search(r"доставлен", st, re.I) or (re.search(r"отгруж", st, re.I) and pickup(oid)):
            t["bought"][0] += 1
            t["bought"][1] += total
        elif re.search(r"отмен|возврат", st, re.I):
            t["cancelled"][0] += 1
            t["cancelled"][1] += total
    done = t["bought"][0] + t["cancelled"][0]
    return {k: {"count": v[0], "sum": v[1]} for k, v in t.items()} | {"buyout": round(100 * t["bought"][0] / done) if done else None,
                                                                       "mirror": mirror.enabled()}


@bp.get("/admin/api/retail-stats")
@admin.guard
def retail_stats():
    period = request.args.get("period", "today")
    if period not in PERIODS:
        period = "today"
    return jsonify(ok=True, period=period, **stats(period))
