"""Цена розничного сайта amura.kz — «оптовый магазин для всех» (владелец 08.11.2026). Опт (opt.amura.kz, оптовые цены МС)
и розничную цену в МойСклад (ею пользуется бутик в Байсате) не трогаем: цена сайта считается при сборке каталога.

Цена = (закупка + чистые + упаковка) / (1 − расходы %), вверх до 10 ₸; не ниже оптовой цены МС + 10 ₸.
Чистые: закупка до 3 500 ₸ — 20 %; от 3 500 ₸ — 15 %, но не меньше 700 ₸; не больше 1 000 ₸; от 20 000 ₸ — не меньше 5 %.
Закупка — buyPrice МС с валютой по курсу МС; число меньше 100 без валюты — доллары. Закупки нет — нынешняя розничная цена
(товар в списке «без закупки»). Все числа — настройки владельца; выключатель «Новая модель цен» — только владелец."""
import json
import math
import threading
import time

from flask import Blueprint, jsonify, request

import inbox
import order_hook as oh

bp = Blueprint("site_price", __name__)
KEY = "site_price"
DEFAULTS = {"on": False, "pack": 167, "fees": 6.5, "low_to": 3500, "low_pct": 20, "pct": 15, "min_net": 700, "max_net": 1000,
            "big_from": 20000, "big_pct": 5, "floor_add": 10, "round": 10}
LABELS = {"pack": "Упаковка, ₸", "fees": "Расходы от цены, % (Freedom 3,5 + налог 2 + снятие 1)", "low_to": "Дешёвые товары — закупка до, ₸",
          "low_pct": "Чистые у дешёвых, %", "pct": "Чистые у остальных, %", "min_net": "Чистые не меньше, ₸", "max_net": "Чистые не больше, ₸",
          "big_from": "Дорогие товары — закупка от, ₸", "big_pct": "Чистые у дорогих не меньше, %", "floor_add": "Не ниже опта + ₸",
          "round": "Округление вверх до, ₸"}
USD_GUESS = 100
_costs = {"at": 0, "map": {}}
_lock = threading.Lock()


def conf():
    with inbox.db() as d:
        raw = inbox.get_setting(d, KEY, "")
    try:
        got = json.loads(raw) if raw else {}
    except ValueError:
        got = {}
    return {**DEFAULTS, **{k: v for k, v in got.items() if k in DEFAULTS}}


def save(b):
    c = conf()
    for k in DEFAULTS:
        if k not in b:
            continue
        if k == "on":
            c["on"] = bool(b["on"])
            continue
        v = float(b[k])
        if v < 0 or (k in ("fees", "low_pct", "pct", "big_pct") and v >= 90) or (k == "round" and not 1 <= v <= 1000):
            raise ValueError(f"«{LABELS[k]}»: недопустимое значение")
        c[k] = v
    with inbox.db() as d:
        inbox.set_setting(d, KEY, json.dumps(c, ensure_ascii=False))
    oh._cache.pop("live", None)                          # каталог пересоберётся с новыми ценами
    return c


# ---------- расчёт ----------
def net(cost, c):
    """Чистыми, ₸, с одной продажи."""
    if cost < c["low_to"]:
        n = cost * c["low_pct"] / 100
    else:
        n = max(cost * c["pct"] / 100, c["min_net"])
    n = min(n, c["max_net"])
    if cost >= c["big_from"]:
        n = max(n, cost * c["big_pct"] / 100)
    return n


def ceil_to(v, step):
    step = step or 1
    return int(math.ceil(round(v / step, 6)) * step)


def price(cost, opt, c):
    """→ (цена сайта, чистыми фактически)."""
    raw = (cost + net(cost, c) + c["pack"]) / (1 - c["fees"] / 100)
    p = ceil_to(raw, c["round"])
    if opt and p < opt + c["floor_add"]:
        p = ceil_to(opt + c["floor_add"], c["round"])
    real = p * (1 - c["fees"] / 100) - cost - c["pack"]
    return p, round(real)


# ---------- закупка из МойСклад ----------
def _currencies():
    rows = oh.ms("GET", "/entity/currency", params={"limit": 100}, timeout=20).get("rows") or []
    return {r["id"]: {"iso": (r.get("isoCode") or r.get("name") or "").upper(), "rate": float(r.get("rate") or 1)} for r in rows}


def _load_costs():
    cur = _currencies()
    usd = next((x["rate"] for x in cur.values() if x["iso"] == "USD"), None)
    out, off = {}, 0
    for _ in range(20):
        rows = oh.ms("GET", "/entity/product", params={"limit": 1000, "offset": off, "filter": "archived=false"}, timeout=60).get("rows") or []
        for r in rows:
            bp_ = r.get("buyPrice") or {}
            v = (bp_.get("value") or 0) / 100
            if v <= 0:
                continue
            cid = (((bp_.get("currency") or {}).get("meta") or {}).get("href") or "").rstrip("/").rsplit("/", 1)[-1]
            c = cur.get(cid) or {"iso": "KZT", "rate": 1}
            if c["iso"] in ("KZT", "398", "ТЕНГЕ", "ТГ") and v < USD_GUESS:
                if usd:
                    out[r["id"]] = (v * usd, f"{v:g} без валюты — доллары по курсу МС {usd:g}")
                continue
            out[r["id"]] = (v * c["rate"], f"{v:g} {c['iso']}" + (f" по курсу МС {c['rate']:g}" if c["iso"] not in ("KZT", "398") else ""))
        if len(rows) < 1000:
            break
        off += 1000
    return out


def costs(wait=False):
    """id → (закупка ₸, пояснение). Обновляется в фоне раз в 30 минут; каталог не ждёт МойСклад."""
    fresh = time.time() - _costs["at"] < 1800
    if not fresh and not _lock.locked():
        def go():
            with _lock:
                try:
                    _costs["map"], _costs["at"] = _load_costs(), time.time()
                    oh._cache.pop("live", None)
                except Exception as e:
                    print("Цены сайта: закупка из МойСклад не получена:", str(e)[:150], flush=True)
        if wait or not _costs["map"]:
            go() if wait else threading.Thread(target=go, daemon=True).start()
        else:
            threading.Thread(target=go, daemon=True).start()
    return _costs["map"]


def rows(items, c=None, cmap=None):
    """Таблица: товар — закупка — опт — старая розница — цена сайта — чистыми. Без закупки — нынешняя розница."""
    c, cmap = c or conf(), costs() if cmap is None else cmap
    out = []
    for i in items:
        cost, note = cmap.get(i["id"], (None, ""))
        old = i.get("rtlMs", i.get("rtl", 0)) or 0
        if cost:
            p, real = price(cost, i.get("opt") or 0, c)
        elif old:
            p, real = old, None
        else:
            continue
        out.append({"id": i["id"], "code": i.get("code", ""), "name": i.get("name", ""), "img": i.get("img"), "cost": round(cost) if cost else None,
                    "costNote": note, "opt": i.get("opt") or 0, "old": old, "new": p, "net": real, "noCost": not cost})
    return out


def apply(data):
    """При включённой модели: rtl товаров = цена сайта (старая розничная — в rtlMs). Закупки ещё нет в памяти — всё как было."""
    c = conf()
    if not c["on"]:
        return data
    cmap = costs()
    if not cmap:
        return data
    for key in ("items", "hiddenItems", "outItems"):
        for i in data.get(key) or []:
            r = rows([i], c, cmap)
            if r:
                i["rtlMs"] = i.get("rtl", 0)
                i["rtl"] = r[0]["new"]
    return data


# ---------- API (только владелец) ----------
def _owner():
    import admin
    me = admin.who()
    if not me:
        return None, (jsonify(ok=False, error="Войдите заново"), 401)
    if me["role"] != "owner":
        return None, (jsonify(ok=False, error="Цены сайта — только для владельца"), 403)
    return me, None


@bp.get("/admin/api/site-price")
def route_get():
    _, err = _owner()
    if err:
        return err
    c = conf()
    cmap = costs(wait=request.args.get("wait") == "1")
    data = oh.live(max_age=600)
    items = (data.get("items") or []) + (data.get("hiddenItems") or [])
    r = rows(items, c, cmap)
    r.sort(key=lambda x: (not x["noCost"], str(x["code"])))
    return jsonify(ok=True, conf=c, labels=LABELS, rows=r, ready=bool(cmap), noCost=sum(1 for x in r if x["noCost"]),
                   costsAt=int(_costs["at"]))


@bp.put("/admin/api/site-price")
def route_put():
    _, err = _owner()
    if err:
        return err
    try:
        c = save(request.get_json(silent=True) or {})
    except (TypeError, ValueError) as e:
        return jsonify(ok=False, error=str(e) or "Числа — цифрами"), 400
    return jsonify(ok=True, conf=c)
