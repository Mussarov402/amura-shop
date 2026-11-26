"""KPI: план продаж и бонусы сотрудников (владелец 26.11.2026). Подключаемый модуль «kpi», по умолчанию выключен.

Данные — только чтение МойСклад: «Прибыльность по сотрудникам» за месяц (выручка, себестоимость, валовая прибыль,
число продаж) и отгрузки/розничные продажи месяца (оплачено / в долг). Сотрудник = владелец документа в МойСклад.
Бонус считается только с оплаченной части: ВП × (оплачено / сумма отгрузок). Учёт зарплаты не строим — только расчёт,
«предварительно, до сверки себестоимости».

Схемы (все параметры — числа в настройках, ничего не зашито):
  pct   — % от оплаченной ВП сверх базы: (ВП − база) × % ;
  steps — ступени к плану: выполнение ≥ порог % → сумма (берётся самая высокая достигнутая);
  none  — оклад без бонуса.
Хранение — настройка системы `kpi_conf`."""
import calendar
import json
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

import inbox
import order_hook as oh

bp = Blueprint("kpi", __name__)
KEY = "kpi_conf"
LOW_MARGIN = 8.0                   # маржа месяца ниже — подозрение на ошибку себестоимости (окт 5,5 %)
HIST = 6                           # месяцев в истории
SCHEMES = ("pct", "steps", "none")

# Черновик по слову владельца (26.11): Руфина — «Mussarov 2-й» в МойСклад; схема ещё выбирается — черновик не начисляет
DRAFT = {"staff": False, "emps": {
    "f001c3c0-c9f6-11f0-0a80-0270000f3f89": {
        "name": "Руфина", "ms": "Mussarov 2-й", "salary": 250000, "scheme": "pct", "base": 1100000, "pct": 15,
        "steps": [], "plans": {"2026-12": 1800000}, "chat": "", "draft": True},
}}

_cache, _lock = {}, threading.Lock()


# ---------- настройки ----------
def conf():
    with inbox.db() as d:
        raw = inbox.get_setting(d, KEY, "")
    try:
        c = json.loads(raw) if raw else json.loads(json.dumps(DRAFT))
    except ValueError:
        c = json.loads(json.dumps(DRAFT))
    c.setdefault("staff", False)
    c.setdefault("emps", {})
    return c


def _save(c):
    with inbox.db() as d:
        inbox.set_setting(d, KEY, json.dumps(c, ensure_ascii=False))


def staff_on():
    try:
        return bool(conf().get("staff"))
    except Exception:
        return False


def _num(v, name, lo=0, hi=10 ** 10):
    try:
        x = float(str(v).replace(" ", "").replace(",", ".")) if str(v).strip() != "" else 0
    except ValueError:
        raise ValueError(f"{name}: нужно число")
    if not lo <= x <= hi:
        raise ValueError(f"{name}: от {lo} до {hi}")
    return round(x, 2) if x != int(x) else int(x)


def set_emp(eid, b):
    eid = str(eid or "").strip()
    if not eid:
        raise ValueError("Выберите сотрудника МойСклад")
    c = conf()
    if b.get("delete"):
        c["emps"].pop(eid, None)
        _save(c)
        return c
    e = c["emps"].setdefault(eid, {"plans": {}, "steps": []})
    if "name" in b:
        e["name"] = str(b["name"] or "").strip()[:60]
    if "ms" in b:
        e["ms"] = str(b["ms"] or "").strip()[:80]
    if "chat" in b:
        e["chat"] = str(b["chat"] or "").strip()[:40]
    if "salary" in b:
        e["salary"] = _num(b["salary"], "Оклад")
    if "scheme" in b:
        if b["scheme"] not in SCHEMES:
            raise ValueError("Неизвестная схема")
        e["scheme"] = b["scheme"]
    if "base" in b:
        e["base"] = _num(b["base"], "База")
    if "pct" in b:
        e["pct"] = _num(b["pct"], "Процент", 0, 100)
    if "steps" in b:
        st = []
        for s in b["steps"] or []:
            if str(s.get("at", "")).strip() == "" and str(s.get("sum", "")).strip() == "":
                continue
            st.append({"at": _num(s.get("at"), "Порог %", 0, 1000), "sum": _num(s.get("sum"), "Сумма ступени")})
        e["steps"] = sorted(st, key=lambda s: s["at"])
    if "plan" in b:
        m = str(b.get("month") or month_key(_now()))
        if len(m) != 7 or m[4] != "-":
            raise ValueError("Месяц: ГГГГ-ММ")
        p = _num(b["plan"], "План ВП")
        e.setdefault("plans", {})
        if p:
            e["plans"][m] = p
        else:
            e["plans"].pop(m, None)
    if "draft" in b:
        e["draft"] = bool(b["draft"])
    _save(c)
    return c


def plan_for(e, m):
    """План месяца: свой или последний заданный раньше."""
    ps = (e or {}).get("plans") or {}
    if m in ps:
        return ps[m]
    prev = [k for k in ps if k < m]
    return ps[max(prev)] if prev else 0


def bonus(e, vp_paid, plan):
    """→ (бонус, пояснение)."""
    s = (e or {}).get("scheme") or "none"
    if s == "pct":
        base, pct = e.get("base") or 0, e.get("pct") or 0
        over = max(0, vp_paid - base)
        return round(over * pct / 100), f"{_fmt_pct(pct)} от оплаченной ВП сверх {_fmt(base)} ₸"
    if s == "steps":
        if not plan:
            return 0, "ступени к плану — план не задан"
        done = vp_paid / plan * 100
        got = [st for st in e.get("steps") or [] if done >= st["at"]]
        if not got:
            return 0, f"ступени к плану — выполнено {done:.0f} %, первая ступень не достигнута"
        st = got[-1]
        return round(st["sum"]), f"ступень {_fmt_pct(st['at'])} плана → {_fmt(st['sum'])} ₸"
    return 0, "оклад без бонуса"


def _fmt(v):
    return f"{round(v):,}".replace(",", " ")


def _fmt_pct(v):
    return (f"{v:g}").replace(".", ",") + " %"


# ---------- даты (магазин — Алматы, МойСклад — Москва, на 2 часа раньше) ----------
def _now():
    return datetime.now(oh.ALMATY)


def month_key(d):
    return d.strftime("%Y-%m")


def _month_bounds(m):
    y, mo = int(m[:4]), int(m[5:7])
    days = calendar.monthrange(y, mo)[1]
    start = datetime(y, mo, 1) - timedelta(hours=2)                 # 00:00 Алматы = 22:00 Москвы накануне
    end = datetime(y, mo, days) + timedelta(days=1) - timedelta(hours=2, seconds=1)
    return start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), days


def _months_back(m, n):
    y, mo = int(m[:4]), int(m[5:7])
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{mo:02d}")
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    return out


# ---------- МойСклад (только чтение) ----------
def _id(meta_holder):
    return str(((meta_holder or {}).get("meta") or {}).get("href", "")).rstrip("/").split("/")[-1].split("?")[0]


def _rows(path, params):
    out, off = [], 0
    while True:
        r = oh.ms("GET", path, params={"limit": 1000, "offset": off, **params}, timeout=60)
        rows = r if isinstance(r, list) else r.get("rows", [])
        out += rows
        off += 1000
        if isinstance(r, list) or off >= (r.get("meta") or {}).get("size", 0) or not rows:
            return out


def _profit(frm, to):
    """{employee_id: {name, sell, cost, ret, retCost, vp, count}} из «Прибыльности по сотрудникам»."""
    out = {}
    for r in _rows("/report/profit/byemployee", {"momentFrom": frm, "momentTo": to}):
        eid = _id(r.get("employee")) or r.get("employeeId") or ""
        if not eid:
            continue
        sell, cost = (r.get("sellSum") or 0) / 100, (r.get("sellCostSum") or 0) / 100
        ret, rcost = (r.get("returnSum") or 0) / 100, (r.get("returnCostSum") or 0) / 100
        out[eid] = {"name": (r.get("employee") or {}).get("name") or r.get("name") or "", "sell": sell, "cost": cost,
                    "ret": ret, "retCost": rcost, "vp": (r.get("profit") or 0) / 100,
                    "count": int(r.get("salesCount") or 0)}
    return out


def _pay(frm, to):
    """{employee_id: {sum, paid, n, nPaid}} по отгрузкам и розничным продажам месяца (владелец документа)."""
    out = {}
    flt = f"moment>={frm};moment<={to};applicable=true"
    for path, retail in (("/entity/demand", False), ("/entity/retaildemand", True)):
        try:
            rows = _rows(path, {"filter": flt})
        except Exception as e:
            if retail:
                print("KPI: розничные продажи не получены:", e, flush=True)
                continue
            raise
        for d in rows:
            eid = _id(d.get("owner"))
            s = (d.get("sum") or 0) / 100
            p = s if retail else min(s, (d.get("payedSum") or 0) / 100)
            x = out.setdefault(eid, {"sum": 0.0, "paid": 0.0, "n": 0, "nPaid": 0})
            x["sum"] += s
            x["paid"] += p
            x["n"] += 1
            x["nPaid"] += 1 if s and p >= s - 0.5 else 0
    return out


def month_data(m, ttl=None):
    """Факт месяца (кэш: текущий месяц — 5 мин, прошлые — 30 мин)."""
    cur = m == month_key(_now())
    ttl = ttl if ttl is not None else (300 if cur else 1800)
    with _lock:
        hit = _cache.get(m)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    frm, to, _ = _month_bounds(m)
    data = {"profit": _profit(frm, to), "pay": _pay(frm, to)}
    with _lock:
        _cache[m] = (time.time(), data)
    return data


# ---------- расчёт ----------
def card(eid, e, m, data, now=None):
    now = now or _now()
    p = data["profit"].get(eid) or {"name": (e or {}).get("ms", ""), "sell": 0, "cost": 0, "ret": 0, "retCost": 0, "vp": 0, "count": 0}
    py = data["pay"].get(eid) or {"sum": 0, "paid": 0, "n": 0, "nPaid": 0}
    _, _, days = _month_bounds(m)
    cur = m == month_key(now)
    day = now.day if cur else days
    net = p["sell"] - p["ret"]
    vp = p["vp"]
    share = (py["paid"] / py["sum"]) if py["sum"] else (1.0 if not p["count"] else 0.0)
    share = max(0.0, min(1.0, share))
    vp_paid = vp * share
    plan = plan_for(e, m) if e else 0
    forecast = vp / day * days if cur and day else vp
    margin = round(vp / net * 100, 1) if net else 0
    b, how = bonus(e, vp_paid, plan) if e else (0, "")
    draft = bool((e or {}).get("draft"))
    if draft:
        b_shown = 0
    else:
        b_shown = b
    salary = (e or {}).get("salary") or 0
    return {"id": eid, "name": (e or {}).get("name") or p["name"], "ms": p["name"] or (e or {}).get("ms", ""),
            "month": m, "plan": plan, "vp": round(vp), "pct": round(vp / plan * 100, 1) if plan else None,
            "forecast": round(forecast), "forecastPct": round(forecast / plan * 100, 1) if plan else None,
            "revenue": round(net), "cost": round(p["cost"] - p["retCost"]), "margin": margin,
            "lowMargin": bool(net) and margin < LOW_MARGIN, "count": p["count"],
            "shipped": round(py["sum"]), "paid": round(py["paid"]), "debt": round(py["sum"] - py["paid"]),
            "nShip": py["n"], "nPaid": py["nPaid"], "vpPaid": round(vp_paid),
            "salary": salary, "bonus": b_shown, "bonusCalc": b, "bonusHow": how, "total": salary + b_shown,
            "scheme": (e or {}).get("scheme") or "", "draft": draft, "setup": bool(e), "day": day, "days": days}


def overview(m=None):
    now = _now()
    m = m or month_key(now)
    c = conf()
    data = month_data(m)
    ids = list(c["emps"]) + [k for k in data["profit"] if k not in c["emps"]]
    cards = [card(i, c["emps"].get(i), m, data, now) for i in ids]
    tot_net = sum(x["revenue"] for x in cards)
    tot_vp = sum(x["vp"] for x in cards)
    return {"month": m, "cards": cards, "company": {"revenue": tot_net, "vp": tot_vp,
            "margin": round(tot_vp / tot_net * 100, 1) if tot_net else 0}, "conf": c}


def history(eid, n=HIST):
    c = conf()
    e = c["emps"].get(eid)
    out = []
    for m in _months_back(month_key(_now()), n):
        try:
            x = card(eid, e, m, month_data(m))
        except Exception as ex:
            out.append({"month": m, "error": str(ex)[:120]})
            continue
        out.append({k: x[k] for k in ("month", "plan", "vp", "pct", "vpPaid", "bonus", "bonusCalc", "revenue", "margin", "lowMargin", "debt", "draft")})
    return out


def employees():
    """Сотрудники МойСклад для выбора в настройках."""
    try:
        return [{"id": r.get("id"), "name": r.get("name") or r.get("fullName") or ""}
                for r in _rows("/entity/employee", {"filter": "archived=false"})]
    except Exception:
        return []


# ---------- API ----------
def _who():
    import admin
    import modules
    me = admin.who()
    if not me:
        return None, (jsonify(ok=False, error="Войдите заново"), 401)
    if not modules.enabled("kpi"):
        return None, (jsonify(ok=False, error="Модуль «KPI» выключен"), 404)
    return me, None


def _own(me, c):
    """Сотрудник видит только свою карточку: связь — «Сотрудник в системе AMURA» в настройках KPI."""
    return next((i for i, e in c["emps"].items() if e.get("chat") and str(e["chat"]) == str(me.get("chat"))), None)


STAFF_KEYS = ("id", "name", "month", "plan", "vp", "pct", "forecast", "forecastPct", "vpPaid", "bonus", "bonusCalc", "bonusHow", "draft", "setup", "day", "days")


@bp.get("/admin/api/kpi")
def route_get():
    me, err = _who()
    if err:
        return err
    m = request.args.get("month") or None
    try:
        if me["role"] != "owner":
            c = conf()
            if not c.get("staff"):
                return jsonify(ok=False, error="Владелец пока не открыл KPI сотрудникам"), 403
            eid = _own(me, c)
            if not eid:
                return jsonify(ok=True, staff=True, cards=[], month=m or month_key(_now()))
            mm = m or month_key(_now())
            x = card(eid, c["emps"][eid], mm, month_data(mm))
            return jsonify(ok=True, staff=True, month=mm, cards=[{k: x[k] for k in STAFF_KEYS}])
        o = overview(m)
    except Exception as e:
        return jsonify(ok=False, error="МойСклад не ответил: " + str(e)[:200]), 503
    import team
    o["team"] = [{"chat": s["chat"], "name": s["name"]} for s in team.staff() if s.get("active")]
    return jsonify(ok=True, staff=False, lowMargin=LOW_MARGIN, **o)


@bp.get("/admin/api/kpi/history")
def route_history():
    me, err = _who()
    if err:
        return err
    eid = request.args.get("id", "")
    if me["role"] != "owner":
        return jsonify(ok=False, error="История — только владелец"), 403
    return jsonify(ok=True, items=history(eid), lowMargin=LOW_MARGIN)


@bp.get("/admin/api/kpi/employees")
def route_employees():
    me, err = _who()
    if err:
        return err
    if me["role"] != "owner":
        return jsonify(ok=False, error="Только владелец"), 403
    return jsonify(ok=True, items=employees())


@bp.put("/admin/api/kpi/settings")
def route_put():
    me, err = _who()
    if err:
        return err
    if me["role"] != "owner":
        return jsonify(ok=False, error="Настройки KPI — только владелец"), 403
    b = request.get_json(silent=True) or {}
    try:
        if "staff" in b and not b.get("id"):
            c = conf()
            c["staff"] = bool(b["staff"])
            _save(c)
        else:
            set_emp(b.get("id"), b)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, conf=conf())
