"""Доставка в корзине розничного сайта (amura.kz/shop) — как на WB: способ получения, цена и срок сразу.

Способы: «Курьер» (по Алматы — Яндекс, с выбором дня и интервала; в другие города — СДЭК до двери),
«Пункт выдачи СДЭК» (список пунктов и постаматов города), «Самовывоз».
Цена для клиента — ступени по сумме товаров из настроек доставки (delivery.price); СДЭК до двери —
всегда дороже на door_extra (по умолчанию 1 000 ₸) и никогда не бесплатна. Сроки СДЭК — из калькулятора СДЭК.
Сервер пересчитывает цену доставки при заказе сам (order_core), цене из браузера не верим.
"""
import re

from flask import Blueprint, jsonify, request

import delivery
import order_hook as oh

bp = Blueprint("checkout", __name__)
bp.after_request(oh.cors)


def is_almaty(city):
    return "алмат" in str(city or "").lower() or "almaty" in str(city or "").lower()


def door_extra():
    return int(delivery.conf()["rules"].get("door_extra", 1000) or 0)


def allowed(city):
    """Способы получения на рознице: Алматы — Яндекс «в течение дня», самовывоз, срочный курьер; другие города — пункт выдачи СДЭК."""
    return ("courier", "pickup", "express") if is_almaty(city) else ("cdek",)


def client_price(goods_sum, method, city):
    """Цена доставки для клиента: у каждого способа своя цена и свой порог бесплатной доставки (Обзор → Доставка)."""
    r = delivery.conf()["rules"]
    if method == "pickup":
        return 0
    if method == "express":
        return int(r["express_price"])
    price, free = (r["sdd_price"], r["sdd_free"]) if method == "courier" else (r["cdek_price"], r["cdek_free"])
    return 0 if free and goods_sum >= free else int(price)


def rates():
    r = delivery.conf()["rules"]
    return {"courier": {"price": r["sdd_price"], "free": r["sdd_free"]}, "express": {"price": r["express_price"], "free": 0},
            "cdek": {"price": r["cdek_price"], "free": r["cdek_free"]}}


# ---------- СДЭК: город, сроки, пункты выдачи (с кэшем — СДЭК отвечает не мгновенно) ----------
def _cdek():
    c = delivery.conf()["svc"]["cdek"]
    if not (c.get("client_id") and c.get("secret")):
        return None
    cd = delivery.Cdek(c)
    tok = oh.cached("cdek_tok", 3000, cd.token)
    return cd, tok


def cdek_city_code(city):
    city = re.sub(r"\s+", " ", str(city or "")).strip()
    if not city:
        return None
    def find():
        x = _cdek()
        if not x:
            return None
        cd, tok = x
        r = oh.requests.get(cd.base + "/location/cities", params={"country_codes": "KZ", "city": city, "size": 1},
                            headers={"Authorization": "Bearer " + tok}, timeout=15)
        r.raise_for_status()
        j = r.json() or []
        return j[0]["code"] if j else None
    return oh.cached("cdek_city:" + city.lower(), 86400, find)


def cdek_eta(city):
    """{"pvz": (мин, макс дней), "door": (мин, макс)} — по самому дешёвому тарифу каждого вида."""
    code = cdek_city_code(city)
    if not code:
        return {}
    def calc():
        cd, tok = _cdek()
        src = cdek_city_code("Алматы")
        r = delivery.conf()["rules"]
        r_ = oh.requests.post(cd.base + "/calculator/tarifflist", headers={"Authorization": "Bearer " + tok}, timeout=20, json={
            "from_location": {"code": src}, "to_location": {"code": code},
            "packages": [{"weight": max(300, r["weight"] * 2), "length": r["box_w"], "width": r["box_h"], "height": r["box_d"]}]})
        r_.raise_for_status()
        best = {}
        for t in r_.json().get("tariff_codes", []):
            kind = "door" if t.get("delivery_mode") in (1, 3) else "pvz" if t.get("delivery_mode") in (2, 4, 6, 7) else None
            if kind and t.get("delivery_sum") is not None and (kind not in best or t["delivery_sum"] < best[kind][0]):
                best[kind] = (t["delivery_sum"], t.get("period_min"), t.get("period_max"))
        return {k: (v[1], v[2]) for k, v in best.items()}
    return oh.cached("cdek_eta:" + str(code), 6 * 3600, calc)


def cdek_points(city):
    code = cdek_city_code(city)
    if not code:
        return []
    def load():
        cd, tok = _cdek()
        r = oh.requests.get(cd.base + "/deliverypoints", params={"city_code": code, "country_code": "KZ"},
                            headers={"Authorization": "Bearer " + tok}, timeout=20)
        r.raise_for_status()
        out = []
        for p in r.json() or []:
            if p.get("is_handout") is False:
                continue
            loc = p.get("location") or {}
            out.append({"code": p.get("code"), "name": p.get("name") or "", "address": loc.get("address") or loc.get("address_full") or "",
                        "type": "Постамат" if p.get("type") == "POSTAMAT" else "Пункт выдачи", "hours": p.get("work_time") or "",
                        "lat": loc.get("latitude"), "lon": loc.get("longitude")})
        out.sort(key=lambda x: x["address"])
        return out[:300]
    return oh.cached("cdek_pvz:" + str(code), 6 * 3600, load)


def _days(p):
    if not p or p[0] is None:
        return ""
    a, b = p
    return f"{a} дн." if not b or a == b else f"{a}–{b} дн."


def options(city, goods_sum):
    c = delivery.conf()
    alm = is_almaty(city)
    eta, err = {}, ""
    if city:
        try:
            eta = cdek_eta(city)
        except Exception as e:
            err = "Сроки СДЭК сейчас недоступны"
            print("Корзина: сроки СДЭК:", str(e)[:200], flush=True)
    slots = delivery.slots_ahead(days=4) if alm else []
    ff = c["rules"]["free_from"]
    rng = lambda p: [p[0], p[1] or p[0]] if p and p[0] is not None else None  # noqa: E731
    all_methods = {
        "courier": {"id": "courier", "name": "В течение дня", "price": client_price(goods_sum, "courier", city),
                    "note": "Яндекс, привезём за 4 часа в выбранное окно", "days": None},
        "pickup": {"id": "pickup", "name": "Самовывоз", "price": 0, "note": c["store"].get("wh_addr") or "Со склада в Алматы", "days": None},
        "express": {"id": "express", "name": "Express", "price": client_price(goods_sum, "express", city),
                    "note": "Яндекс Экспресс, за 1–2 часа", "days": None},
        "cdek": {"id": "cdek", "name": "Пункт выдачи СДЭК", "price": client_price(goods_sum, "cdek", city),
                 "note": "Пункты и постаматы СДЭК" + (f", {_days(eta.get('pvz'))}" if eta.get("pvz") else ""), "days": rng(eta.get("pvz"))},
    }
    methods = [all_methods[m] for m in allowed(city)]
    return {"almaty": alm, "methods": methods, "rates": rates(), "slots": slots, "freeFrom": ff, "doorExtra": door_extra(),
            "tiers": c["tiers"], "hours": c["store"].get("wh_hours") or "", "warn": err}


@bp.get("/delivery/options")
def options_route():
    try:
        s = max(0, int(float(request.args.get("sum", 0) or 0)))
    except ValueError:
        s = 0
    return jsonify(ok=True, **options(str(request.args.get("city", ""))[:80], s))


@bp.get("/delivery/pvz")
def pvz_route():
    city = str(request.args.get("city", ""))[:80]
    try:
        pts = cdek_points(city)
    except Exception as e:
        print("Корзина: пункты СДЭК:", str(e)[:200], flush=True)
        return jsonify(ok=False, error="Список пунктов сейчас недоступен — впишите адрес пункта вручную", points=[])
    return jsonify(ok=True, points=pts)


@bp.route("/me/retail", methods=["POST", "OPTIONS"])
def me_retail():
    """Клиент вошёл на розничном сайте — метка «розница» в МойСклад (видна в панели → Клиенты)."""
    if request.method == "OPTIONS":
        return "", 204
    cid = oh.session_cid()
    if not cid:
        return jsonify(ok=False, error="Войдите заново"), 401
    try:
        oh.mark_retail(cid)
    except Exception as e:
        print("Розница: метка покупателя не поставлена:", str(e)[:200], flush=True)
        return jsonify(ok=False, error="Не получилось, попробуем позже"), 502
    return jsonify(ok=True)


# ---------- для order_core: розничный заказ ----------
def order_delivery(d, ship, city, goods_sum):
    """(цена, текст для описания, ошибка) для розничного заказа; цена считается здесь, не в браузере."""
    address = str(d.get("address", "")).strip()[:200]
    if ship not in allowed(city):
        return 0, "", ("По Алматы — доставка в течение дня, срочный курьер или самовывоз" if is_almaty(city)
                       else "В ваш город доставляем в пункт выдачи СДЭК")
    if ship == "pickup":
        return 0, "Самовывоз", None
    if ship == "cdek":
        pvz = d.get("pvz") or {}
        code = re.sub(r"[^\w-]", "", str(pvz.get("code", "")))[:20]
        where = str(pvz.get("address") or address).strip()[:200]
        if not where:
            return 0, "", "Выберите пункт выдачи СДЭК"
        return client_price(goods_sum, "cdek", city), f"Пункт выдачи СДЭК — {where}" + (f" (код {code})" if code else ""), None
    if not address:
        return 0, "", "Укажите адрес доставки"
    if ship == "express":
        return client_price(goods_sum, "express", city), f"Срочный курьер по Алматы (Яндекс Экспресс) — {address}", None
    if is_almaty(city):
        slot = d.get("slot") or {}
        when = ""
        if slot.get("date") and slot.get("from"):
            ok = any(s["date"] == slot["date"] and s["from"] == slot.get("from") for s in delivery.slots_ahead(days=7))
            if not ok:
                return 0, "", "Этот интервал уже недоступен — выберите другой"
            when = f", {slot['date'][8:10]}.{slot['date'][5:7]} {slot.get('name', '')} {slot['from']}–{slot.get('to', '')}".rstrip()
        return client_price(goods_sum, "courier", city), f"Курьер по Алматы (Яндекс) — {address}{when}", None
    return client_price(goods_sum, "courier", city), f"Курьер СДЭК до двери — {address}", None
