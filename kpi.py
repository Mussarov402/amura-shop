"""KPI: план продаж и бонусы сотрудников (владелец 26.11.2026). Подключаемый модуль «kpi», по умолчанию выключен.

Сотрудник KPI = пользователь системы AMURA (team.py; владелец — тоже). Продажи сотрудника:
  1) заказы, которые он создал или провёл в системе AMURA («Новый заказ», заказ из чата, статус «Подтвержден»/«Собран») —
     заказ закрепляется за первым, кто это сделал (таблица kpi_order, в МойСклад не пишем);
  2) переходный период — документы МойСклад под его «доп. логином МС» (напр. Руфина = «Mussarov 2-й»), если заказ
     не закреплён в системе за кем-то. Каждая отгрузка считается один раз.
Факт — по отгрузкам и розничным продажам месяца (Алматы) в МойСклад: выручка = сумма документа, себестоимость — как в
«Экономике розницы» (себестоимость товара МойСклад), ВП = выручка − себестоимость. Бонус — только с оплаченных отгрузок
(розничная продажа — оплачена). Учёт зарплаты не строим — только расчёт, «предварительно, до сверки себестоимости».

Схемы (все параметры — числа в настройках, ничего не зашито):
  pct   — % от оплаченной ВП сверх базы: (ВП − база) × % ;
  steps — ступени к плану: выполнение ≥ порог % → сумма (берётся самая высокая достигнутая);
  none  — оклад без бонуса.
Доступ сотрудника к своей карточке — право «KPI (своя карточка)» в «Команде». Хранение — настройка `kpi_conf`."""
import calendar
import json
import secrets
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
MARK_STATES = ("Подтвержден", "Собран")   # «провёл заказ» в системе AMURA

# Черновик по слову владельца (26.11): Руфина, в МойСклад пока работает под «Mussarov 2-й» (admin2); схема ещё выбирается —
# черновик не начисляет. Сотрудника системы AMURA владелец выбирает в настройках.
DRAFT = {"emps": {
    "ruf": {"name": "Руфина", "chat": "", "msExtra": "f001c3c0-c9f6-11f0-0a80-0270000f3f89", "msExtraName": "Mussarov 2-й",
            "salary": 250000, "scheme": "pct", "base": 1100000, "pct": 15, "steps": [], "plans": {"2026-12": 1800000}, "draft": True},
}}

_cache, _lock, _ready = {}, threading.Lock(), [False]


# ---------- закрепление заказов за сотрудником системы AMURA ----------
def _table(d):
    if not _ready[0]:
        d.run("CREATE TABLE IF NOT EXISTS kpi_order (number TEXT PRIMARY KEY, chat TEXT, name TEXT, how TEXT, at DOUBLE PRECISION)")
        _ready[0] = True


def attribute(number, chat, name, how):
    """Закрепить заказ за сотрудником (первый, кто создал или провёл, — навсегда)."""
    number, chat = str(number or "").strip(), str(chat or "").strip()
    if not number or not chat:
        return
    try:
        with inbox.db() as d:
            _table(d)
            d.run("INSERT INTO kpi_order (number, chat, name, how, at) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (number) DO NOTHING",
                  (number, chat, str(name or "")[:80], str(how or "")[:60], time.time()))
    except Exception as e:
        print("KPI: заказ не закреплён:", e, flush=True)


def mark(number, how, me=None):
    """Из панели: кто сейчас вошёл — тот и закрепляет заказ."""
    if me is None:
        try:
            import admin
            me = admin.who()
        except Exception:
            me = None
    if me and me.get("chat"):
        attribute(number, me["chat"], me.get("name"), how)


def attributed():
    """{номер заказа: chat сотрудника}."""
    try:
        with inbox.db() as d:
            _table(d)
            return {r[0]: r[1] for r in d.run("SELECT number, chat FROM kpi_order", many=True)}
    except Exception as e:
        print("KPI: закрепления не прочитаны:", e, flush=True)
        return {}


# ---------- настройки ----------
def conf():
    with inbox.db() as d:
        raw = inbox.get_setting(d, KEY, "")
    try:
        c = json.loads(raw) if raw else json.loads(json.dumps(DRAFT))
    except ValueError:
        c = json.loads(json.dumps(DRAFT))
    c.setdefault("emps", {})
    return c


def _save(c):
    with inbox.db() as d:
        inbox.set_setting(d, KEY, json.dumps(c, ensure_ascii=False))
    with _lock:
        _cache.clear()


def _num(v, name, lo=0, hi=10 ** 10):
    try:
        x = float(str(v).replace(" ", "").replace(",", ".")) if str(v).strip() != "" else 0
    except ValueError:
        raise ValueError(f"{name}: нужно число")
    if not lo <= x <= hi:
        raise ValueError(f"{name}: от {lo} до {hi}")
    return round(x, 2) if x != int(x) else int(x)


def set_emp(eid, b):
    c = conf()
    eid = str(eid or "").strip()
    if b.get("delete"):
        c["emps"].pop(eid, None)
        _save(c)
        return c, eid
    if not eid:
        eid = "e" + secrets.token_hex(4)
    e = c["emps"].setdefault(eid, {"plans": {}, "steps": [], "scheme": "pct"})
    if "chat" in b:
        chat = str(b["chat"] or "").strip()[:40]
        if chat and any(k != eid and x.get("chat") == chat for k, x in c["emps"].items()):
            raise ValueError("Этот сотрудник уже есть в KPI")
        e["chat"] = chat
    if "name" in b:
        e["name"] = str(b["name"] or "").strip()[:60]
    if "msExtra" in b:
        e["msExtra"] = str(b["msExtra"] or "").strip()[:40]
        e["msExtraName"] = str(b.get("msExtraName") or "").strip()[:80] if e["msExtra"] else ""
        if e["msExtra"] and any(k != eid and x.get("msExtra") == e["msExtra"] for k, x in c["emps"].items()):
            raise ValueError("Этот логин МойСклад уже привязан к другому сотруднику")
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
    if not e.get("chat") and not e.get("msExtra"):
        raise ValueError("Выберите сотрудника системы AMURA (или логин МойСклад)")
    _save(c)
    return c, eid


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


def _rows(path, params, page=1000):
    out, off = [], 0
    while True:
        r = oh.ms("GET", path, params={"limit": page, "offset": off, **params}, timeout=60)
        rows = r if isinstance(r, list) else r.get("rows", [])
        out += rows
        off += page
        if isinstance(r, list) or off >= (r.get("meta") or {}).get("size", 0) or not rows:
            return out


def _company(frm, to):
    """Итоги компании из «Прибыльности по сотрудникам»: выручка (за вычетом возвратов) и ВП."""
    sell = vp = 0.0
    for r in _rows("/report/profit/byemployee", {"momentFrom": frm, "momentTo": to}):
        sell += ((r.get("sellSum") or 0) - (r.get("returnSum") or 0)) / 100
        vp += (r.get("profit") or 0) / 100
    return {"revenue": round(sell), "vp": round(vp), "margin": round(vp / sell * 100, 1) if sell else 0}


def _unit_cost(pid):
    import retail_econ
    v, _ = retail_econ._cost(pid)
    return v


def _doc_cost(kind, did):
    """Себестоимость документа: Σ количество × себестоимость товара МойСклад (кэш 1 час). → (сумма, позиций без себестоимости)."""
    k = ("pos", kind, did)
    with _lock:
        hit = _cache.get(k)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    cost, miss = 0.0, 0
    for p in _rows(f"/entity/{kind}/{did}/positions", {}):
        a = p.get("assortment") or {}
        if (a.get("meta") or {}).get("type") == "service":
            continue
        v = _unit_cost(_id(a))
        if v is None:
            miss += 1
        else:
            cost += v * (p.get("quantity") or 0)
    with _lock:
        _cache[k] = (time.time(), (cost, miss))
    return cost, miss


def _docs(frm, to):
    """Отгрузки (с номером заказа) и розничные продажи месяца."""
    flt = f"moment>={frm};moment<={to};applicable=true"
    out = []
    for d in _rows("/entity/demand", {"filter": flt, "expand": "customerOrder"}, page=100):
        s = (d.get("sum") or 0) / 100
        out.append({"kind": "demand", "id": d.get("id"), "order": str((d.get("customerOrder") or {}).get("name") or ""),
                    "owner": _id(d.get("owner")), "sum": s, "paid": min(s, (d.get("payedSum") or 0) / 100)})
    try:
        for d in _rows("/entity/retaildemand", {"filter": flt}):
            s = (d.get("sum") or 0) / 100
            out.append({"kind": "retaildemand", "id": d.get("id"), "order": "", "owner": _id(d.get("owner")), "sum": s, "paid": s})
    except Exception as e:
        print("KPI: розничные продажи не получены:", e, flush=True)
    return out


def assign(docs, emps, attr):
    """{id сотрудника KPI: [документы]} — каждый документ не больше одного раза.
    Заказ, закреплённый в системе AMURA, — у того, за кем закреплён; незакреплённый — по доп. логину МойСклад."""
    by_chat = {str(e.get("chat")): k for k, e in emps.items() if e.get("chat")}
    by_ms = {e.get("msExtra"): k for k, e in emps.items() if e.get("msExtra")}
    out = {k: [] for k in emps}
    for d in docs:
        who = attr.get(d["order"]) if d["order"] else None
        k = by_chat.get(str(who)) if who else by_ms.get(d["owner"])
        if k:
            out[k].append(d)
    return out


def month_data(m, ttl=None):
    """Документы месяца и итоги компании (кэш: текущий месяц — 5 мин, прошлые — 30 мин)."""
    cur = m == month_key(_now())
    ttl = ttl if ttl is not None else (300 if cur else 1800)
    with _lock:
        hit = _cache.get(m)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    frm, to, _ = _month_bounds(m)
    data = {"docs": _docs(frm, to), "company": _company(frm, to)}
    with _lock:
        _cache[m] = (time.time(), data)
    return data


# ---------- расчёт ----------
def card(eid, e, m, docs, attr, now=None):
    now = now or _now()
    _, _, days = _month_bounds(m)
    cur = m == month_key(now)
    day = now.day if cur else days
    rev = sum(d["sum"] for d in docs)
    paid = sum(d["paid"] for d in docs)
    cost, miss = 0.0, 0
    for d in docs:
        c, x = _doc_cost(d["kind"], d["id"])
        cost += c
        miss += x
    vp = rev - cost
    share = max(0.0, min(1.0, paid / rev)) if rev else 0.0
    vp_paid = vp * share
    plan = plan_for(e, m)
    forecast = vp / day * days if cur and day else vp
    margin = round(vp / rev * 100, 1) if rev else 0
    b, how = bonus(e, vp_paid, plan)
    draft = bool(e.get("draft"))
    b_shown = 0 if draft else b
    salary = e.get("salary") or 0
    return {"id": eid, "name": e.get("name") or "", "chat": e.get("chat") or "", "msExtraName": e.get("msExtraName") or "",
            "month": m, "plan": plan, "vp": round(vp), "pct": round(vp / plan * 100, 1) if plan else None,
            "forecast": round(forecast), "forecastPct": round(forecast / plan * 100, 1) if plan else None,
            "revenue": round(rev), "cost": round(cost), "noCost": miss, "margin": margin,
            "lowMargin": bool(rev) and margin < LOW_MARGIN, "count": len(docs),
            "fromSystem": sum(1 for d in docs if d["order"] and d["order"] in attr),
            "paid": round(paid), "debt": round(rev - paid),
            "nPaid": sum(1 for d in docs if d["sum"] and d["paid"] >= d["sum"] - 0.5),
            "vpPaid": round(vp_paid), "salary": salary, "bonus": b_shown, "bonusCalc": b, "bonusHow": how,
            "total": salary + b_shown, "scheme": e.get("scheme") or "", "draft": draft, "day": day, "days": days}


def _cards(m, c, data, attr, only=None, now=None):
    parts = assign(data["docs"], c["emps"], attr)
    return [card(k, e, m, parts.get(k, []), attr, now) for k, e in c["emps"].items() if only is None or k == only]


def overview(m=None):
    now = _now()
    m = m or month_key(now)
    c = conf()
    data = month_data(m)
    return {"month": m, "cards": _cards(m, c, data, attributed(), now=now), "company": data["company"], "conf": c}


def history(eid, n=HIST):
    c = conf()
    if eid not in c["emps"]:
        return []
    attr, out = attributed(), []
    for m in _months_back(month_key(_now()), n):
        try:
            x = _cards(m, c, month_data(m), attr, only=eid)[0]
        except Exception as ex:
            out.append({"month": m, "error": str(ex)[:120]})
            continue
        out.append({k: x[k] for k in ("month", "plan", "vp", "pct", "vpPaid", "bonus", "bonusCalc", "revenue", "margin",
                                      "lowMargin", "debt", "draft", "count")})
    return out


def ms_employees():
    """Логины МойСклад для «доп. логина» в настройках."""
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
    return next((k for k, e in c["emps"].items() if e.get("chat") and str(e["chat"]) == str(me.get("chat"))), None)


STAFF_KEYS = ("id", "name", "month", "plan", "vp", "pct", "forecast", "forecastPct", "vpPaid", "bonus", "bonusCalc", "bonusHow",
              "draft", "day", "days")


def _team():
    import team
    rows = [{"chat": str(oh.OWNER), "name": "Владелец", "kpi": True}]
    return rows + [{"chat": s["chat"], "name": s["name"], "kpi": "kpi" in s["perms"]} for s in team.staff() if s.get("active")]


@bp.get("/admin/api/kpi")
def route_get():
    me, err = _who()
    if err:
        return err
    m = request.args.get("month") or None
    try:
        if me["role"] != "owner":
            if "kpi" not in me["perms"]:
                return jsonify(ok=False, error="Нет доступа к KPI — его даёт владелец в «Команде»"), 403
            c = conf()
            eid = _own(me, c)
            mm = m or month_key(_now())
            if not eid:
                return jsonify(ok=True, staff=True, cards=[], month=mm)
            x = _cards(mm, c, month_data(mm), attributed(), only=eid)[0]
            return jsonify(ok=True, staff=True, month=mm, cards=[{k: x[k] for k in STAFF_KEYS}])
        o = overview(m)
    except Exception as e:
        return jsonify(ok=False, error="МойСклад не ответил: " + str(e)[:200]), 503
    return jsonify(ok=True, staff=False, lowMargin=LOW_MARGIN, team=_team(), **o)


@bp.get("/admin/api/kpi/history")
def route_history():
    me, err = _who()
    if err:
        return err
    if me["role"] != "owner":
        return jsonify(ok=False, error="История — только владелец"), 403
    return jsonify(ok=True, items=history(request.args.get("id", "")), lowMargin=LOW_MARGIN)


@bp.get("/admin/api/kpi/employees")
def route_employees():
    me, err = _who()
    if err:
        return err
    if me["role"] != "owner":
        return jsonify(ok=False, error="Только владелец"), 403
    return jsonify(ok=True, items=ms_employees())


@bp.put("/admin/api/kpi/settings")
def route_put():
    me, err = _who()
    if err:
        return err
    if me["role"] != "owner":
        return jsonify(ok=False, error="Настройки KPI — только владелец"), 403
    b = request.get_json(silent=True) or {}
    try:
        c, eid = set_emp(b.get("id"), b)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, id=eid, conf=c)
