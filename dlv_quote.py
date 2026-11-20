"""Настоящая цена доставки на розничном сайте (решение владельца 20.11.2026): покупатель платит столько, сколько стоит доставка.

- Яндекс «в течение дня» и Express: бесплатный расчёт check-price (заявку не создаёт) от точки склада до адреса покупателя,
  тем же классом, что потом вызывает система (courier; «в течение дня» — с интервалом выбранного окна).
- СДЭК: тариф из Алматы до пункта выдачи / постамата по калькулятору (calculator/tarifflist), вес — по товарам корзины
  (вес из карточки МойСклад, без веса — 0,5 кг за штуку).
- Запас на скачок цены — «Настройки → Доставка»: к Яндексу +ya_margin % (по умолчанию 10) и вверх до 50 ₸, к СДЭК +cdek_margin %
  (по умолчанию 0) и вверх до целого тенге.
- Бесплатно от порога free_all, кроме Express. API не ответил за WAIT секунд — фиксированная цена из настроек (запись в лог);
  расчёт при этом продолжается в фоне и попадает в кэш.
- Кэш: адрес + день + окно (или город + вес для СДЭК) — 15 минут. Заказ (checkout.order_delivery) берёт цену отсюда же,
  поэтому сумма в счёте и в МойСклад = цена на экране; не совпала с показанной — заказ не принимаем, покупатель видит новую цену."""
import hashlib
import math
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

import delivery
import order_hook as oh

WAIT = 4.5                 # сек: дольше покупатель не ждёт — показываем фиксированную цену
TTL = 900                  # кэш расчёта, сек
KG_DEFAULT = 0.5           # вес штуки без веса в карточке
PAUSE = 120                # после сбоя связи со службой — 2 минуты сразу фиксированная цена
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="dlvq")
_down = {"ya": 0.0, "cdek": 0.0}
_inflight = {}             # ключ расчёта → идущий расчёт: второй запрос того же адреса ждёт его, а не создаёт вторую заявку
_flock = threading.Lock()


def parcel_kg(lines):
    """Вес посылки, кг: из карточек МойСклад (oh._weights), без веса — 0,5 кг за штуку."""
    w = getattr(oh, "_weights", {}) or {}
    kg = sum(max(1, int(l.get("qty") or 1)) * (float(w.get(l.get("id")) or 0) or KG_DEFAULT) for l in lines or [])
    return round(max(0.1, kg or KG_DEFAULT), 2)


def _ceil(x, step):
    return int(math.ceil(round(x, 2) / step) * step)


def with_margin(raw, kind):
    r = delivery.conf()["rules"]
    pct = r.get("ya_margin", 10) if kind == "ya" else r.get("cdek_margin", 0)
    v = raw * (1 + pct / 100)
    return _ceil(v, 50) if kind == "ya" else _ceil(v, 1)


def _iv(slot):
    """Окно сайта («2026-11-21», «14:00»–«18:00» по Алматы) → интервал Яндекса в ISO с поясом +05:00."""
    try:
        a = datetime.strptime(f"{slot['date']} {slot['from']}", "%Y-%m-%d %H:%M").replace(tzinfo=oh.ALMATY)
        b = datetime.strptime(f"{slot['date']} {slot['to']}", "%Y-%m-%d %H:%M").replace(tzinfo=oh.ALMATY)
        return {"from": a.isoformat(), "to": b.isoformat()}
    except (KeyError, TypeError, ValueError):
        return None


def _ya_interval(slot, wh):
    """Окно «в течение дня» ровно в том виде, как его даёт Яндекс (/delivery-methods → same_day_delivery.available_intervals):
    интервал Яндекса того же дня, внутри которого начинается окно сайта. Свой интервал Яндекс не принимает и считает обычный тариф."""
    import courier
    st = delivery.conf()["store"]
    m = oh.cached("ya_methods_q", 300, lambda: courier.ya("/delivery-methods", {"start_point": list(wh), "fullname": "Алматы, " + (st.get("wh_addr") or "")}))
    sd = m.get("same_day_delivery") or {}
    if sd.get("allowed") is False:
        raise RuntimeError("Яндекс: «в течение дня» для склада недоступна")
    want = _iv(slot)
    if not want:
        return None
    w0 = datetime.fromisoformat(want["from"])
    best = None
    for i in sd.get("available_intervals") or []:
        try:
            a, b = datetime.fromisoformat(str(i["from"]).replace("Z", "+00:00")), datetime.fromisoformat(str(i["to"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if a.astimezone(oh.ALMATY).date() != w0.date():
            continue
        if a <= w0 < b:
            return {"from": i["from"], "to": i["to"]}
        if best is None or abs((a - w0).total_seconds()) < abs((best[0] - w0).total_seconds()):
            best = (a, i)
    if best:
        return {"from": best[1]["from"], "to": best[1]["to"]}
    raise RuntimeError(f"Яндекс не даёт окно «в течение дня» на {slot.get('date')}")


def _box():
    r = delivery.conf()["rules"]
    return r["box_d"], r["box_w"], r["box_h"]


def _ya_raw(address, slot, kg):
    import courier
    tok = delivery.conf()["svc"]["yandex"].get("token")
    if not tok:
        raise RuntimeError("нет ключа Яндекс Доставки")
    pt = courier.geocode(address)
    if not pt:
        raise LookupError("адрес не найден на карте")
    st = delivery.conf()["store"]
    wh = delivery.resolve_coords(st.get("wh_coords")) or courier.geocode(st.get("wh_addr", ""))
    if not wh:
        raise RuntimeError("нет координат склада")
    l, w, h = _box()
    # «в течение дня» — свой тариф: только same_day_data с интервалом Яндекса, без taxi_class (как claim_body в courier.py);
    # Express — taxi_class courier (мото / вело / пешком), как автовызов Express
    req = {"same_day_data": {"delivery_interval": _ya_interval(slot, wh)}} if slot else {"taxi_class": "courier"}
    body = {"items": [{"quantity": 1, "weight": kg, "size": {"length": l / 100, "width": w / 100, "height": h / 100}}],
            "route_points": [{"coordinates": list(wh)}, {"coordinates": list(pt)}], "requirements": req}
    try:
        r = requests.post(courier.YA + "/check-price", json=body, timeout=WAIT,
                          headers={"Authorization": "Bearer " + tok, "Accept-Language": "ru"})
    except requests.RequestException:
        _down["ya"] = time.time()
        raise
    if r.status_code >= 500:
        _down["ya"] = time.time()
    if r.status_code >= 400:
        raise RuntimeError(f"Яндекс {r.status_code}: {r.text[:150]}")
    j = r.json()
    if slot:                                       # для сверки в логе Render: что ответил Яндекс на «в течение дня»
        print(f"Доставка: check-price «в течение дня» {req['same_day_data']['delivery_interval']} → {j.get('price')} "
              f"(ответ: {', '.join(sorted(j))[:200]})", flush=True)
    return float(j["price"])


def _nap(sec):
    time.sleep(sec)


CLAIM_WAIT = 25            # оценка заявки «в течение дня» у Яндекса идёт несколько секунд; ждём в фоне до 25 с


def _sdd_claim_raw(address, slot, kg):
    """«В течение дня» — по оценке настоящей заявки, как вкладка «Курьер» (check-price даёт для этого тарифа не ту цену,
    проверка 20.11.2026: 5 550 ₸ против 3 100 ₸ у Express). Заявка создаётся, Яндекс считает цену (estimating →
    ready_for_approval), цену забираем и сразу отменяем заявку бесплатно. Кэш TTL — одна заявка на адрес + окно за 15 минут."""
    import courier
    pt = courier.geocode(address)
    if not pt:
        raise LookupError("адрес не найден на карте")
    wh = courier.warehouse()
    iv = _ya_interval(slot, wh["pt"])
    num = "est-" + hashlib.sha1(f"{address}|{slot}".encode()).hexdigest()[:8]
    o = {"num": num, "sum": 0, "name": "Оценка цены (сайт)", "phone": wh["phone"], "addr": address, "qty": 1}
    body = courier.claim_body(o, pt, wh, iv)
    body["items"][0]["weight"] = kg
    body["comment"] = "AMURA: оценка цены для покупателя на сайте, заявка будет отменена"
    j = courier.ya("/claims/create", body, request_id=str(uuid.uuid4()))
    cid, price, info = j.get("id"), None, j
    try:
        t0 = time.time()
        while time.time() - t0 < CLAIM_WAIT:
            price = courier._price(info)
            st = info.get("status", "")
            if price or st in ("estimating_failed", "failed", "cancelled"):
                break
            _nap(1)
            info = courier.ya("/claims/info", claim_id=cid)
    finally:
        try:                                          # заявку отменяем всегда (бесплатно, пока курьер не назначен)
            cur = courier.ya("/claims/info", claim_id=cid)
            if cur.get("status") not in ("cancelled", "estimating_failed", "failed"):
                courier.ya("/claims/cancel", {"version": cur.get("version", 1), "cancel_state": cur.get("available_cancel_state") or "free"}, claim_id=cid)
        except Exception as e:
            print(f"Доставка: оценочная заявка {cid} не отменена: {str(e)[:150]}", flush=True)
    print(f"Доставка: «в течение дня» по оценке заявки {cid} {iv} → {price}", flush=True)
    if not price:
        raise RuntimeError(f"Яндекс не оценил заявку «в течение дня» ({info.get('status', '')})")
    return float(price)


def _cdek_raw(city, kg, postamat):
    import checkout
    code, src = checkout.cdek_city_code(city), checkout.cdek_city_code("Алматы")
    if not code or not src:
        raise LookupError("город не найден в СДЭК")
    x = checkout._cdek()
    if not x:
        raise RuntimeError("нет ключей СДЭК")
    cd, tok = x
    l, w, h = _box()
    try:
        r = requests.post(cd.base + "/calculator/tarifflist", headers={"Authorization": "Bearer " + tok}, timeout=WAIT, json={
            "from_location": {"code": src}, "to_location": {"code": code},
            "packages": [{"weight": int(kg * 1000), "length": l, "width": w, "height": h}]})
        r.raise_for_status()
    except requests.RequestException:
        _down["cdek"] = time.time()
        raise
    best = {}
    for t in r.json().get("tariff_codes", []):
        mode, s = t.get("delivery_mode"), t.get("delivery_sum")
        if s is None:
            continue
        kind = "pvz" if mode in (2, 4) else "post" if mode in (6, 7) else None
        if kind:
            best[kind] = min(float(s), best.get(kind, float("inf")))
    if not best:
        raise RuntimeError("СДЭК не дал тариф до пункта выдачи")
    return best.get("post" if postamat else "pvz") or min(best.values())


def _key(method, city, address, slot, pvz, kg):
    if method == "cdek":
        return f"dq:cdek:{str(city).lower().strip()}:{kg}:{1 if _postamat(pvz) else 0}"
    when = f"{(slot or {}).get('date', '')}/{(slot or {}).get('from', '')}" if method == "courier" else "x"
    addr = re.sub(r"\s+", " ", str(address).lower()).strip()
    return f"dq:{method}:{addr}:{when}:{kg}"


def _postamat(pvz):
    return "постамат" in str((pvz or {}).get("type", "")).lower()


def quote(method, city, goods, lines, address="", slot=None, pvz=None):
    """{"price": ₸ для покупателя, "real": True — по API службы, False — фиксированная из настроек, "raw": цена службы}."""
    import checkout
    if method == "pickup":
        return {"price": 0, "real": True, "raw": 0}
    fixed = checkout.client_price(goods, method, city)
    free = checkout.free_all()
    if method != "express" and free and goods >= free:
        return {"price": 0, "real": True, "raw": 0}
    alm = checkout.is_almaty(city)
    if method == "courier" and not (slot and slot.get("date")):      # окно не выбрано — цену «в течение дня» считаем под окно
        return {"price": fixed, "real": False, "raw": 0}
    if method in ("courier", "express") and not (alm and str(address).strip()):
        return {"price": fixed, "real": False, "raw": 0}
    if method not in ("courier", "express", "cdek") or (method == "cdek" and not str(city).strip()):
        return {"price": fixed, "real": False, "raw": 0}
    kind = "cdek" if method == "cdek" else "ya"
    kg = parcel_kg(lines)
    key = _key(method, city, address, slot, pvz, kg)
    hit = oh._cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        raw = hit[1]
    else:
        if time.time() - _down[kind] < PAUSE:
            print(f"Доставка: {method} — служба недавно не ответила, фиксированная цена {fixed} ₸", flush=True)
            return {"price": fixed, "real": False, "raw": 0}
        calc = (lambda: _cdek_raw(city, kg, _postamat(pvz))) if kind == "cdek" else \
               (lambda: _sdd_claim_raw(address, slot, kg)) if method == "courier" and slot else \
               (lambda: _ya_raw(address, None, kg))
        with _flock:
            fut = _inflight.get(key)
            if fut is None or fut.done():
                fut = _inflight[key] = _pool.submit(oh.cached, key, TTL, calc)
        try:
            raw = fut.result(timeout=WAIT)
        except Exception as e:
            print(f"Доставка: цена {method} по API не получена ({str(e)[:150] or type(e).__name__}) — фиксированная {fixed} ₸", flush=True)
            return {"price": fixed, "real": False, "raw": 0}
    return {"price": with_margin(raw, kind), "real": True, "raw": round(raw)}
