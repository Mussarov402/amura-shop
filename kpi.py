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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

import inbox
import order_hook as oh

bp = Blueprint("kpi", __name__)
KEY = "kpi_conf"
LOW_MARGIN = 8.0                   # маржа месяца ниже — подозрение на ошибку себестоимости (окт 5,5 %)
HIST = 6                           # месяцев в истории
SCHEMES = ("pct", "steps", "none")
MARK_STATES = ("Подтвержден", "Собран")
SNAP = "kpi_snap:"                # снимок фактов месяца в настройках системы
FRESH = 600                        # текущий месяц пересчитывается в фоне раз в 10 минут
WORKERS = 4                        # параллельных запросов к МойСклад при расчёте   # «провёл заказ» в системе AMURA

# Черновик по слову владельца (26.11): Руфина, в МойСклад пока работает под «Mussarov 2-й» (admin2); схема ещё выбирается —
# черновик не начисляет. Сотрудника системы AMURA владелец выбирает в настройках.
DRAFT = {"emps": {
    "ruf": {"name": "Руфина", "chat": "", "msExtra": "f001c3c0-c9f6-11f0-0a80-0270000f3f89", "msExtraName": "Mussarov 2-й",
            "salary": 250000, "scheme": "pct", "base": 1100000, "pct": 15, "steps": [], "plans": {"2026-12": 1800000}, "draft": True},
}}

_cache, _lock, _ready = {}, threading.Lock(), [False]
_running, _errors, _poller = set(), {}, [None]


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


def _par(fn, items):
    """Параллельно, не больше WORKERS запросов к МойСклад сразу (лимит МС — 5; общий семафор в order_hook.ms)."""
    items = list(items)
    if len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(fn, items))


def _positions(doc):
    k = ("pos", doc["kind"], doc["id"])
    with _lock:
        hit = _cache.get(k)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    rows = [((_id(p.get("assortment"))), p.get("quantity") or 0)
            for p in _rows(f"/entity/{doc['kind']}/{doc['id']}/positions", {})
            if ((p.get("assortment") or {}).get("meta") or {}).get("type") != "service"]
    with _lock:
        _cache[k] = (time.time(), rows)
    return rows


def _costs(docs):
    """{id документа: (себестоимость, позиций без себестоимости)} — позиции и себестоимость товаров пачками, параллельно."""
    pos = dict(zip([d["id"] for d in docs], _par(_positions, docs)))
    pids = sorted({pid for rows in pos.values() for pid, _ in rows if pid})
    unit = dict(zip(pids, _par(_unit_cost, pids)))
    out = {}
    for did, rows in pos.items():
        cost, miss = 0.0, 0
        for pid, q in rows:
            v = unit.get(pid)
            if v is None:
                miss += 1
            else:
                cost += v * q
        out[did] = (cost, miss)
    return out


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


def _sig(c):
    """Кто чьи продажи: меняется — факты пересчитываются."""
    return json.dumps(sorted([k, str(e.get("chat") or ""), str(e.get("msExtra") or "")] for k, e in c["emps"].items()))


def compute(m, c=None):
    """Факты месяца по каждому сотруднику KPI (без плана и бонуса — они из настроек на лету). Долго: только в фоне."""
    c = c or conf()
    t0 = time.time()
    frm, to, _ = _month_bounds(m)
    docs = _docs(frm, to)
    attr = attributed()
    parts = assign(docs, c["emps"], attr)
    mine = [d for ds in parts.values() for d in ds]
    costs = _costs(mine)
    emps = {}
    for k, ds in parts.items():
        emps[k] = {"rev": sum(d["sum"] for d in ds), "paid": sum(d["paid"] for d in ds),
                   "cost": sum(costs[d["id"]][0] for d in ds), "miss": sum(costs[d["id"]][1] for d in ds),
                   "count": len(ds), "fromSystem": sum(1 for d in ds if d["order"] and d["order"] in attr),
                   "nPaid": sum(1 for d in ds if d["sum"] and d["paid"] >= d["sum"] - 0.5)}
    snap = {"month": m, "at": time.time(), "secs": round(time.time() - t0, 1), "sig": _sig(c),
            "company": _company(frm, to), "emps": emps}
    with inbox.db() as d:
        inbox.set_setting(d, SNAP + m, json.dumps(snap, ensure_ascii=False))
    print(f"KPI: {m} посчитан за {snap['secs']} с, документов сотрудников {len(mine)}", flush=True)
    return snap


def snapshot(m):
    with inbox.db() as d:
        raw = inbox.get_setting(d, SNAP + m, "")
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _job(m):
    try:
        compute(m)
    except Exception as e:
        print(f"KPI: {m} не посчитан:", str(e)[:300], flush=True)
        with _lock:
            _errors[m] = (time.time(), str(e)[:200])
    finally:
        with _lock:
            _running.discard(m)


def _spawn(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True, name="kpi-" + "-".join(map(str, args))).start()


def ensure(m, force=False):
    """Снимок месяца свежий? Нет — пересчёт в фоне (один на месяц). → идёт ли расчёт."""
    s = snapshot(m)
    cur = m == month_key(_now())
    stale = (s is None or force or s.get("sig") != _sig(conf())
             or (cur and time.time() - (s.get("at") or 0) > FRESH))
    if not stale:
        return False
    with _lock:
        if m in _running:
            return True
        if not force and m in _errors and time.time() - _errors[m][0] < FRESH:
            return False                     # МойСклад не ответил — следующая попытка через 10 минут (или «Обновить»)
        _running.add(m)
        _errors.pop(m, None)
    _spawn(_job, m)
    return True


def _poll():
    time.sleep(90)
    while True:
        try:
            import modules
            if modules.enabled("kpi"):
                ensure(month_key(_now()))
        except Exception as e:
            print("KPI: фоновый пересчёт:", e, flush=True)
        time.sleep(FRESH)


def start_poller():
    """Пересчёт текущего месяца раз в FRESH секунд, пока модуль включён (экран всегда открывается сразу)."""
    import bg
    if bg.in_master():
        bg.start(start_poller)
        return
    if bg.STANDBY:
        return
    if _poller[0] is None:
        _poller[0] = threading.Thread(target=_poll, daemon=True, name="kpi-poll")
        _poller[0].start()


# ---------- расчёт карточки (быстро, из снимка) ----------
EMPTY = {"rev": 0, "paid": 0, "cost": 0, "miss": 0, "count": 0, "fromSystem": 0, "nPaid": 0}


def card(eid, e, m, f, now=None):
    now = now or _now()
    _, _, days = _month_bounds(m)
    cur = m == month_key(now)
    day = now.day if cur else days
    f = f or EMPTY
    rev, paid, cost = f["rev"], f["paid"], f["cost"]
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
            "revenue": round(rev), "cost": round(cost), "noCost": f["miss"], "margin": margin,
            "lowMargin": bool(rev) and margin < LOW_MARGIN, "count": f["count"], "fromSystem": f["fromSystem"],
            "paid": round(paid), "debt": round(rev - paid), "nPaid": f["nPaid"],
            "vpPaid": round(vp_paid), "salary": salary, "bonus": b_shown, "bonusCalc": b, "bonusHow": how,
            "total": salary + b_shown, "scheme": e.get("scheme") or "", "draft": draft, "day": day, "days": days,
            "pending": f is EMPTY}


def view(m, only=None, force=False):
    """Ответ экрана сразу: последний снимок + идёт ли пересчёт. Без снимка — ready=False («Считаю…»)."""
    c = conf()
    computing = ensure(m, force)
    s = snapshot(m)
    with _lock:
        err = (_errors.get(m) or (0, ""))[1]
    base = {"month": m, "computing": computing, "error": err if not s else "", "ready": bool(s)}
    if not s:
        return {**base, "cards": [], "company": None, "at": None}
    emps = s.get("emps") or {}
    cards = [card(k, e, m, emps.get(k)) for k, e in c["emps"].items() if only is None or k == only]
    at = datetime.fromtimestamp(s["at"], oh.ALMATY).strftime("%d.%m %H:%M")
    return {**base, "cards": cards, "company": s.get("company"), "at": at, "secs": s.get("secs")}


def history(eid, n=HIST):
    """Из снимков месяцев (прошлые месяцы не меняются — считаются один раз); недостающие — в фоне, по одному."""
    c = conf()
    e = c["emps"].get(eid)
    if not e:
        return [], False
    out, pending = [], False
    for m in _months_back(month_key(_now()), n):
        s = snapshot(m)
        if not s or s.get("sig") != _sig(c):
            pending = True
            _queue_hist(m)
            if not s:
                out.append({"month": m, "pending": True})
                continue
        x = card(eid, e, m, (s.get("emps") or {}).get(eid))
        out.append({k: x[k] for k in ("month", "plan", "vp", "pct", "vpPaid", "bonus", "bonusCalc", "revenue", "margin",
                                      "lowMargin", "debt", "draft", "count")})
    return out, pending


_hist_q = []


def _queue_hist(m):
    """Прошлые месяцы — по одному за раз, чтобы не занимать МойСклад."""
    with _lock:
        if m in _hist_q or m in _running:
            return
        _hist_q.append(m)
        start = len(_hist_q) == 1
    if start:
        _spawn(_hist_worker)


def _hist_worker():
    while True:
        with _lock:
            if not _hist_q:
                return
            m = _hist_q[0]
            busy = m in _running
            if not busy:
                _running.add(m)
        if not busy:
            _job(m)
        with _lock:
            _hist_q.pop(0)


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
              "draft", "day", "days", "pending")


def _team():
    import team
    rows = [{"chat": str(oh.OWNER), "name": "Владелец", "kpi": True}]
    return rows + [{"chat": s["chat"], "name": s["name"], "kpi": "kpi" in s["perms"]} for s in team.staff() if s.get("active")]


def _month_arg():
    m = request.args.get("month") or month_key(_now())
    return m if len(m) == 7 and m[4] == "-" and m[:4].isdigit() and m[5:].isdigit() else month_key(_now())


@bp.get("/admin/api/kpi")
def route_get():
    """Отвечает сразу из снимка; пересчёт — в фоне (раз в 10 минут, по кнопке «Обновить» — сразу)."""
    me, err = _who()
    if err:
        return err
    m = _month_arg()
    if me["role"] != "owner":
        if "kpi" not in me["perms"]:
            return jsonify(ok=False, error="Нет доступа к KPI — его даёт владелец в «Команде»"), 403
        eid = _own(me, conf())
        if not eid:
            return jsonify(ok=True, staff=True, ready=True, cards=[], month=m)
        v = view(m, only=eid)
        v["cards"] = [{k: x[k] for k in STAFF_KEYS} for x in v["cards"]]
        v.pop("company", None)
        return jsonify(ok=True, staff=True, **v)
    v = view(m, force=request.args.get("refresh") == "1")
    return jsonify(ok=True, staff=False, lowMargin=LOW_MARGIN, team=_team(), conf=conf(), **v)


@bp.get("/admin/api/kpi/history")
def route_history():
    me, err = _who()
    if err:
        return err
    if me["role"] != "owner":
        return jsonify(ok=False, error="История — только владелец"), 403
    items, pending = history(request.args.get("id", ""))
    return jsonify(ok=True, items=items, pending=pending, lowMargin=LOW_MARGIN)


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
