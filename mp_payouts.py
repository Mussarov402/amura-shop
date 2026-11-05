"""Выплаты Ozon и WB → МойСклад (схема Финансиста, 04.11.2026). Сначала «на сухую»: владелец видит, какие документы создадутся,
и проводит выплату кнопкой; автоматически — только после его «да» (настройка mp_payout_auto, по умолчанию выключено).

Одна выплата площадки (WB — отчёт реализации realizationreport_id; Ozon — период cash-flow-statement):
- «Входящий платёж» от контрагента площадки: сумма — продажи по ценам отгрузок (не «к выплате»), operations — отгрузки заказов
  (linkedSum = цена продажи), incomingNumber = ID отчёта/периода. Платёж с таким номером уже есть — не создаём.
- «Исходящие платежи» тому же контрагенту, та же дата: по одному на статью «МП: комиссия», «МП: логистика и доставка», «МП: хранение»,
  «МП: эквайринг», «МП: реклама», «МП: штрафы и прочие удержания» (статьи заводим, если нет).
- Проверка: приход − возвраты − удержания = выплата (±1 ₸), иначе ничего не проводим, тревога.
- Возвраты: «Возврат покупателя» оформляет склад (или списание, если товар не вернулся); система его не создаёт. Продажа и возврат
  в одной выплате — продажа не в приход, платежа нет. Продажа была раньше — исходящий платёж площадке со статьёй «Возврат»,
  operations = возврат покупателя. Возврата по заказу в МойСклад ещё нет — выплату не проводим («нужен возврат по заказу №…»).
- Компенсации и доплаты площадки — в приход (без привязки к отгрузкам).
- Рубли: курс = ₸ зачислено / RUB к выплате; зачисление в ₸ неизвестно — курс покупки МиГ с пометкой «оценка».
- Счёт организации — настройка mp_payout_account (пока владелец не ответил — пусто, проводить нельзя)."""
import time
from datetime import datetime, timedelta

from flask import jsonify, request

import marketplaces as mpk
import order_hook as oh

ITEMS = {"commission": "МП: комиссия", "logistics": "МП: логистика и доставка", "storage": "МП: хранение", "acquiring": "МП: эквайринг",
         "ads": "МП: реклама", "other": "МП: штрафы и прочие удержания"}
RETURN_ITEM_ID = "8dbf99a0-0a01-11e4-a743-002590a32f46"   # статья МойСклад «Возврат» (не расход) — удержание суммы возврата (Финансист 04.11.2026)
OZ_SERVICE = (("storage", ("storage", "placement", "хранен", "размещ")), ("acquiring", ("acquiring", "эквайр")),
              ("ads", ("marketing", "promotion", "advert", "premium", "pay_per", "click", "cpc", "реклам", "продвиж", "клик", "трафарет")),
              ("logistics", ("deliv", "logist", "directflow", "lastmile", "return", "dropoff", "pickup", "drop_off", "flow", "доставк",
                             "логист", "обработк", "processing")))
DAYS = 35


def _cur(v):
    v = str(v or "").strip().lower()
    return "RUB" if v.startswith(("руб", "rub", "rur", "₽")) else "KZT" if not v or v.startswith(("kzt", "тг", "тенге", "₸")) else v.upper()


def _cat(name):
    n = str(name or "").lower()
    return next((k for k, words in OZ_SERVICE if any(w in n for w in words)), "other")


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------- отчёты площадок → выплаты в одном виде ----------
def wb_payouts(ch, d1, d2):
    """WB: детализация отчёта реализации → {id: выплата}. Суммы в валюте отчёта (currency_name)."""
    rows, rrd = [], 0
    for _ in range(50):
        part = ch.req("GET", "statistics", "/api/v5/supplier/reportDetailByPeriod",
                      params={"dateFrom": d1, "dateTo": d2, "rrdid": rrd, "limit": 100000}) or []
        if not isinstance(part, list) or not part:
            break
        rows += part
        rrd = part[-1].get("rrd_id") or 0
        if len(part) < 100000:
            break
    out = {}
    for r in rows:
        rid = str(r.get("realizationreport_id") or "")
        if not rid:
            continue
        p = out.setdefault(rid, {"id": rid, "date": str(r.get("create_dt") or r.get("rr_dt") or r.get("date_to") or "")[:10],
                                 "cur": _cur(r.get("currency_name")),
                                 "sales": [], "returns": [], "out": {k: 0.0 for k in ITEMS}, "payout": 0.0, "comp": 0.0})
        kind, oper = str(r.get("doc_type_name") or ""), str(r.get("supplier_oper_name") or "")
        order = str(r.get("assembly_id") or r.get("srid") or "")
        price, forpay = _f(r.get("retail_price_withdisc_rub")), _f(r.get("ppvz_for_pay"))
        acq = _f(r.get("acquiring_fee"))
        if kind == "Продажа" and oper.startswith("Продажа"):
            p["sales"].append({"order": order, "sum": price})
            p["out"]["commission"] += price - forpay - acq
            p["out"]["acquiring"] += acq
            p["payout"] += forpay
        elif kind == "Возврат" and oper.startswith("Возврат"):
            p["returns"].append({"order": order, "sum": price})
            p["out"]["commission"] -= price - forpay - acq
            p["out"]["acquiring"] -= acq
            p["payout"] -= forpay
        for key, field in (("logistics", "delivery_rub"), ("storage", "storage_fee"), ("other", "penalty"), ("other", "deduction"),
                           ("other", "acceptance")):
            v = _f(r.get(field))
            p["out"][key] += v
            p["payout"] -= v
        add = _f(r.get("additional_payment"))                  # доплата/компенсация WB — в приход
        p["comp"] += add
        p["payout"] += add
    return out


def ozon_payouts(ch, d1, d2):
    """Ozon: периоды выплат (/v1/finance/cash-flow-statement/list) и операции (/v3/finance/transaction/list) → {id: выплата}."""
    flows = []
    for page in range(1, 20):
        r = ch.call("/v1/finance/cash-flow-statement/list", {"date": {"from": d1 + "T00:00:00Z", "to": d2 + "T23:59:59Z"},
                                                             "with_details": False, "page": page, "page_size": 50})
        res = r.get("result") or r
        part = res.get("cash_flows") or []
        flows += part
        if page >= int(res.get("page_count") or 1) or not part:
            break
    out = {}
    for fl in flows:
        per = fl.get("period") or {}
        pid, b, e = str(per.get("id") or ""), str(per.get("begin") or "")[:10], str(per.get("end") or "")[:10]
        if not pid or not b:
            continue
        p = {"id": pid, "date": e or b, "cur": str(fl.get("currency_code") or "RUB").upper(), "sales": [], "returns": [],
             "out": {k: 0.0 for k in ITEMS}, "payout": 0.0, "comp": 0.0}
        ops = []
        for page in range(1, 50):
            r = ch.call("/v3/finance/transaction/list", {"filter": {"date": {"from": b + "T00:00:00.000Z", "to": e + "T23:59:59.999Z"},
                                                                    "transaction_type": "all"}, "page": page, "page_size": 1000})
            res = r.get("result") or r
            part = res.get("operations") or []
            ops += part
            if page >= int(res.get("page_count") or 1) or not part:
                break
        for op in ops:
            post = str((op.get("posting") or {}).get("posting_number") or "")
            sale = _f(op.get("accruals_for_sale"))
            if sale > 0:
                p["sales"].append({"order": post, "sum": sale})
            elif sale < 0:
                p["returns"].append({"order": post, "sum": -sale})
            p["out"]["commission"] -= _f(op.get("sale_commission"))
            for s in op.get("services") or []:
                p["out"][_cat(s.get("name"))] -= _f(s.get("price"))
            known = sale + _f(op.get("sale_commission")) + sum(_f(s.get("price")) for s in op.get("services") or [])
            rest = _f(op.get("amount")) - known                 # прочие начисления/удержания операции (компенсации, штрафы)
            if rest > 0.005:                                    # компенсация (потеря, брак) — в приход
                p["comp"] += rest
            elif rest < -0.005:
                p["out"]["ads" if _cat(op.get("operation_type_name") or op.get("operation_type")) == "ads" else "other"] -= rest
            p["payout"] += _f(op.get("amount"))
        out[pid] = p
    return out


# ---------- план документов ----------
def _demand(ch, ext):
    rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"externalCode={ch.key}-{ext}", "limit": 1}).get("rows") or []
    if not rows:
        return None
    dem = [(x.get("meta") or {}).get("href", "").rstrip("/").rsplit("/", 1)[-1] for x in rows[0].get("demands") or []]
    return {"order": rows[0].get("name") or "", "demand": dem[0] if dem else ""}


def _salesreturn(demand_id):
    """Возврат покупателя по отгрузке, оформленный в МойСклад (склад или вручную)."""
    d = oh.ms("GET", f"/entity/demand/{demand_id}")
    r = [(x.get("meta") or {}).get("href", "").rstrip("/").rsplit("/", 1)[-1] for x in d.get("returns") or []]
    return r[0] if r else ""


def plan(ch, p, rate=None, rate_note=""):
    """Выплата площадки → документы МойСклад в тенге (без записи). ok=False — проводить нельзя (расхождение, нет счёта и т. д.)."""
    import mp_calc
    if p["cur"] not in ("KZT", "398"):
        if rate is None:
            rate, note = mp_calc.to_kzt(1, p["cur"])
            rate_note = f"оценка: {note}" if rate else note
        if not rate:
            return {**p, "ok": False, "why": f"нет курса {p['cur']}: {rate_note}"}
    else:
        rate, rate_note = 1.0, ""
    k = lambda v: round(v * rate)                                   # noqa: E731
    sales, missing = [], []
    for s in p["sales"]:
        d = _demand(ch, s["order"]) if s["order"] else None
        if not d or not d["demand"]:
            missing.append(s["order"] or "без номера")
        sales.append({"order": s["order"], "sum": k(s["sum"]), "ms": (d or {}).get("order", ""), "demand": (d or {}).get("demand", "")})
    returns, need = [], []
    sold = {s["order"] for s in sales}
    for s in p["returns"]:
        d = _demand(ch, s["order"]) if s["order"] else None
        sr = _salesreturn(d["demand"]) if d and d["demand"] else ""
        if not sr:
            need.append((d or {}).get("order") or s["order"] or "без номера")
        returns.append({"order": s["order"], "sum": k(s["sum"]), "ms": (d or {}).get("order", ""), "demand": (d or {}).get("demand", ""),
                        "salesreturn": sr, "same": s["order"] in sold})
    ret_orders = {r["order"] for r in returns}
    comp = k(p.get("comp") or 0)
    income = sum(s["sum"] for s in sales if s["order"] not in ret_orders) + comp     # продажа, вернувшаяся в этом же отчёте, — не в приход
    ret_other = sum(r["sum"] for r in returns if not r["same"])
    outs = {ITEMS[key]: k(v) for key, v in p["out"].items() if round(v * rate) != 0}
    payout = k(p["payout"])
    diff = income - ret_other - sum(outs.values()) - payout
    ok, why = True, ""
    if abs(diff) > 1:
        ok, why = False, f"не сходится на {diff} ₸: приход {income} − возвраты {ret_other} − удержания {sum(outs.values())} ≠ выплата {payout}"
    if need:
        ok, why = False, why or "нужен возврат по заказу " + ", ".join("№" + n for n in need[:10]) + " — оформите возврат покупателя (или списание) в МойСклад"
    if any(v < 0 for v in outs.values()):
        neg = ", ".join(f"{n} {v}" for n, v in outs.items() if v < 0)
        ok, why = False, why or f"отрицательная статья (доплата площадки): {neg} — проверьте вручную"
    return {**p, "ok": ok, "why": why, "rate": rate, "rateNote": rate_note, "income": max(income, 0),
            "salesKzt": sales, "returnsKzt": returns, "outs": outs, "payoutKzt": payout, "missing": missing, "diff": diff,
            "compKzt": comp, "needReturn": need}


# ---------- запись в МойСклад (только по кнопке владельца или после его «да» на автомат) ----------
def _item(name):
    rows = oh.ms("GET", "/entity/expenseitem", params={"filter": f"name={name}", "limit": 5}).get("rows") or []
    hit = next((r for r in rows if r.get("name") == name), None)
    return (hit or oh.ms("POST", "/entity/expenseitem", json={"name": name}))["id"]


def _exists(kind, ext):
    return bool(oh.ms("GET", f"/entity/{kind}", params={"filter": f"externalCode={ext}", "limit": 1}).get("rows"))


def apply(ch, pl, who=""):
    """Провести выплату: приход, удержания, возвраты. Повтор не создаёт вторых документов (externalCode / incomingNumber)."""
    acc = mpk.setting("mp_payout_account", "")
    if not acc:
        raise ValueError("Не выбран счёт для выплат площадок (настройка «счёт для выплат»)")
    if not pl["ok"]:
        raise ValueError(pl["why"])
    org = ch.org()
    base = {"organization": oh.meta("organization", org), "agent": oh.meta("counterparty", mpk.agent(ch)), "applicable": True,
            "moment": pl["date"] + " 12:00:00",
            "organizationAccount": {"meta": {"href": f"{oh.API}/entity/organization/{org}/accounts/{acc}", "type": "account",
                                             "mediaType": "application/json"}}}
    note = f"{ch.title}: выплата {pl['id']}" + (f", курс {pl['rate']:g} ₸ — оценка ({pl['rateNote']}), пересчитать по зачислению"
                                                  if pl["rate"] != 1 else "") + (f"; компенсации площадки {pl['compKzt']} ₸" if pl.get("compKzt") else "")
    made = []
    ext = f"mp-{ch.key}-{pl['id']}"
    dup = oh.ms("GET", "/entity/paymentin", params={"filter": f"incomingNumber={pl['id']}", "limit": 1}).get("rows") or []
    if pl["income"] > 0 and not dup and not _exists("paymentin", ext):
        ret_orders = {r["order"] for r in pl["returnsKzt"]}
        ops = [{**oh.meta("demand", s["demand"]), "linkedSum": s["sum"] * 100} for s in pl["salesKzt"] if s["demand"] and s["order"] not in ret_orders]
        r = oh.ms("POST", "/entity/paymentin", json={**base, "externalCode": ext, "sum": pl["income"] * 100, "incomingNumber": str(pl["id"]),
                                                     "operations": ops, "paymentPurpose": note[:255],
                                                     "description": (note + (f"\nНе найдены в МойСклад: {', '.join(pl['missing'][:30])}"
                                                                             if pl["missing"] else "") + (f"\n{who}" if who else ""))[:4000]})
        made.append(f"Входящий платёж №{r.get('name')} на {oh.fmt(pl['income'])} ₸")
    for name, s in pl["outs"].items():
        e = f"{ext}-{list(ITEMS.values()).index(name)}"
        if s > 0 and not _exists("paymentout", e):
            r = oh.ms("POST", "/entity/paymentout", json={**base, "externalCode": e, "sum": s * 100, "expenseItem": oh.meta("expenseitem", _item(name)),
                                                          "paymentPurpose": f"{name} — {note}"[:255], "description": note})
            made.append(f"Исходящий платёж №{r.get('name')} «{name}» {oh.fmt(s)} ₸")
    for rt in pl["returnsKzt"]:                          # продажа была в прошлой выплате: удержание суммы возврата — статья «Возврат»
        e = f"{ext}-r-{rt['order']}"
        if rt["same"] or not rt["salesreturn"] or _exists("paymentout", e):
            continue
        r = oh.ms("POST", "/entity/paymentout", json={
            **base, "externalCode": e, "sum": rt["sum"] * 100, "expenseItem": oh.meta("expenseitem", RETURN_ITEM_ID),
            "operations": [{**oh.meta("salesreturn", rt["salesreturn"]), "linkedSum": rt["sum"] * 100}],
            "paymentPurpose": f"Возврат по заказу {rt['ms'] or rt['order']} — {note}"[:255], "description": note})
        made.append(f"Исходящий платёж №{r.get('name')} «Возврат» по заказу {rt['ms'] or rt['order']} {oh.fmt(rt['sum'])} ₸")
    print(f"Выплаты {ch.title}: {pl['id']} проведена — " + "; ".join(made), flush=True)
    return made


# ---------- система AMURA ----------
def _period():
    t = datetime.now(mpk.ALMATY).date()
    return (t - timedelta(days=DAYS)).isoformat(), t.isoformat()


def list_plans(ch):
    d1, d2 = _period()
    raw = wb_payouts(ch, d1, d2) if isinstance(ch, mpk.WB) else ozon_payouts(ch, d1, d2)
    out = []
    for pid, p in sorted(raw.items(), key=lambda x: x[1]["date"], reverse=True):
        done = bool(oh.ms("GET", "/entity/paymentin", params={"filter": f"incomingNumber={pid}", "limit": 1}).get("rows")) \
            or _exists("paymentin", f"mp-{ch.key}-{pid}")
        out.append({**plan(ch, p), "done": done})
    return out


def route_list():
    me, err = mpk._me(owner_only=True)
    if err:
        return err
    ch = {c.key: c for c in mpk.channels()}.get(request.args.get("shop", ""))
    if not ch or not ch.keys_set():
        return jsonify(ok=False, error="Нет такого магазина"), 404
    try:
        items = list_plans(ch)
    except mpk.ApiError as e:
        return jsonify(ok=False, error=f"{ch.title}: {e.message}"), 502
    try:
        import payments
        accounts = [a for a in payments.meta()["accounts"] if a["id"] != payments.CASH]
    except Exception as e:
        accounts = []
        print("Выплаты: счета организации не получены:", str(e)[:150], flush=True)
    return jsonify(ok=True, items=items, accounts=accounts, account=mpk.setting("mp_payout_account", ""),
                   auto=mpk.setting("mp_payout_auto", "0") == "1")


def route_apply():
    me, err = mpk._me(owner_only=True)
    if err:
        return err
    b = request.get_json(silent=True) or {}
    ch = {c.key: c for c in mpk.channels()}.get(str(b.get("shop") or ""))
    if not ch:
        return jsonify(ok=False, error="Нет такого магазина"), 404
    try:
        pl = next((x for x in list_plans(ch) if str(x["id"]) == str(b.get("id"))), None)
        if not pl:
            return jsonify(ok=False, error="Выплата не найдена — обновите страницу"), 404
        if pl["done"]:
            return jsonify(ok=False, error="Эта выплата уже проведена"), 409
        made = apply(ch, pl, who=me.get("name", ""))
    except (ValueError, mpk.ApiError) as e:
        return jsonify(ok=False, error=getattr(e, "message", str(e))), 400
    return jsonify(ok=True, made=made)


def route_settings():
    me, err = mpk._me(owner_only=True)
    if err:
        return err
    b = request.get_json(silent=True) or {}
    if "account" in b:
        mpk.set_setting("mp_payout_account", str(b["account"] or ""))
    if "auto" in b:
        if b["auto"] and not mpk.setting("mp_payout_account", ""):
            return jsonify(ok=False, error="Сначала выберите счёт для выплат"), 400
        mpk.set_setting("mp_payout_auto", "1" if b["auto"] else "0")
    return jsonify(ok=True)


def auto_run():
    """Раз в сутки, если владелец включил: новые сходящиеся выплаты проводятся; не сходящиеся — тревога, без документов."""
    if mpk.setting("mp_payout_auto", "0") != "1" or not mpk.setting("mp_payout_account", ""):
        return
    for ch in mpk.channels():
        if not ch.keys_set():
            continue
        try:
            for pl in list_plans(ch):
                if pl["done"]:
                    continue
                if not pl["ok"]:
                    oh.alert(f"mp-payout:{ch.key}:{pl['id']}", f"{ch.title}: выплата {pl['id']} не проведена — {pl['why']}", every=7 * 86400)
                    continue
                apply(ch, pl, who="автоматически")
                if pl["missing"]:
                    oh.alert(f"mp-payout-miss:{ch.key}:{pl['id']}", f"{ch.title}: в выплате {pl['id']} нет в МойСклад заказов: "
                                                                     + ", ".join(pl["missing"][:20]), every=7 * 86400)
        except Exception as e:
            print(f"Выплаты {ch.title}: {str(e)[:200]}", flush=True)


mpk.bp.add_url_rule("/admin/api/mp/payouts", view_func=route_list, endpoint="payouts_list")
mpk.bp.add_url_rule("/admin/api/mp/payouts/apply", view_func=route_apply, endpoint="payouts_apply", methods=["POST"])
mpk.bp.add_url_rule("/admin/api/mp/payouts/settings", view_func=route_settings, endpoint="payouts_settings", methods=["POST"])
