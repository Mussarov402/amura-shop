"""Реальная стоимость доставки по адресам прошлых заказов: Яндекс (Алматы, курьер) и СДЭК (регионы).

Берём заказы с сайта из МойСклад (в описании «Город: …» и «Отправка: …»):
- по Алматы — адреса «Курьер по городу — …» → координаты (OpenStreetMap) → цена Яндекса от склада до адреса;
- по регионам — города → коды СДЭК → тарифы из Алматы: до пункта выдачи / постамата и до двери.
Две посылки: 0,5 кг и 1,5 кг, коробка — из настроек доставки. Итог — в настройке dlv_estimate и одной строкой в лог.
Ничего не заказывает и не создаёт: только расчёт цены.
"""
import json
import re
import statistics
import threading
import time

import requests

import delivery
import inbox
import order_hook as oh

UA = {"User-Agent": "AMURA-admin/1.0 (amura.kz; delivery estimate)"}
ALMATY = (76.9286, 43.2567)                 # центр Алматы (долгота, широта) — если адрес склада не нашёлся
PROFILES = (("0,5 кг", 0.5), ("1,5 кг", 1.5))
MAX_POINTS = 25
_lock = threading.Lock()
_geo = {}


def _orders(limit=400):
    rows, off = [], 0
    while len(rows) < limit:
        part = oh.ms("GET", "/entity/customerorder", params={"filter": "description~Отправка: ", "order": "moment,desc",
                                                              "limit": 100, "offset": off}, timeout=30).get("rows", [])
        rows += part
        if len(part) < 100:
            break
        off += 100
    return rows


def parse(desc):
    """(город, способ, адрес) из описания заказа сайта."""
    d = desc or ""
    city = (re.search(r"^Город:\s*(.+)$", d, re.M) or [None, ""])[1].strip()
    ship = (re.search(r"^Отправка:\s*(.+)$", d, re.M) or [None, ""])[1].strip()
    way, _, rest = ship.partition(" — ")
    return city, way.strip(), rest.strip()


def geocode(q):
    """Координаты адреса через OpenStreetMap (не чаще раза в секунду — правило сервиса)."""
    if q in _geo:
        return _geo[q]
    time.sleep(1.1)
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search", params={"q": q, "format": "json", "limit": 1, "countrycodes": "kz"},
                         headers=UA, timeout=15)
        j = r.json() if r.ok else []
        _geo[q] = (float(j[0]["lon"]), float(j[0]["lat"])) if j else None
    except Exception:
        _geo[q] = None
    return _geo[q]


def _clean_addr(a):
    a = re.sub(r"\b(кв|квартира|подъезд|подьезд|этаж|эт|блок|офис)\.?\s*[\w/-]+", " ", a, flags=re.I)
    return re.sub(r"\s+", " ", a).strip(" ,.")


def yandex_price(token, a, b, kg, box):
    body = {"items": [{"quantity": 1, "weight": kg, "size": {"length": box[0] / 100, "width": box[1] / 100, "height": box[2] / 100}}],
            "route_points": [{"coordinates": list(a)}, {"coordinates": list(b)}], "requirements": {"taxi_class": "courier"}}
    r = requests.post(delivery.Yandex.BASE + "/b2b/cargo/integration/v2/check-price", json=body,
                      headers={"Authorization": "Bearer " + token, "Accept-Language": "ru"}, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Яндекс {r.status_code}: {r.text[:150]}")
    j = r.json()
    return float(j["price"]), (j.get("currency_rules") or {}).get("code", "")


def cdek_city(c, tok, name):
    r = requests.get(c.base + "/location/cities", params={"country_codes": "KZ", "city": name, "size": 1},
                     headers={"Authorization": "Bearer " + tok}, timeout=20)
    r.raise_for_status()
    j = r.json() or []
    return j[0]["code"] if j else None


def cdek_prices(c, tok, src, dst, kg, box):
    """(до пункта выдачи / постамата, до двери) — минимальный тариф каждого вида; вид — delivery_mode СДЭК."""
    r = requests.post(c.base + "/calculator/tarifflist", headers={"Authorization": "Bearer " + tok}, timeout=20, json={
        "from_location": {"code": src}, "to_location": {"code": dst},
        "packages": [{"weight": int(kg * 1000), "length": box[0], "width": box[1], "height": box[2]}]})
    r.raise_for_status()
    pvz, door, cur = [], [], ""
    for t in r.json().get("tariff_codes", []):
        mode, s = t.get("delivery_mode"), t.get("delivery_sum")
        if s is None:
            continue
        cur = t.get("currency") or cur
        (door if mode in (1, 3) else pvz if mode in (2, 4, 6, 7) else []).append(float(s))
    return (min(pvz) if pvz else None), (min(door) if door else None), cur


def _stats(vals, weights=None):
    if not vals:
        return None
    ws = weights or [1] * len(vals)
    flat = sorted(v for v, w in zip(vals, ws) for _ in range(w))
    return {"n": len(vals), "orders": sum(ws), "avg": round(sum(flat) / len(flat)), "med": round(statistics.median(flat)),
            "p90": round(flat[min(len(flat) - 1, int(len(flat) * 0.9))]), "min": round(flat[0]), "max": round(flat[-1])}


def run():
    c = delivery.conf()
    rules, store = c["rules"], c["store"]
    box = (rules["box_w"], rules["box_h"], rules["box_d"])
    res = {"at": time.time(), "box": box, "errors": [], "yandex": {}, "cdek_pvz": {}, "cdek_door": {}, "samples": []}
    orders = _orders()
    almaty, regions = [], {}
    for o in orders:
        city, way, addr = parse(o.get("description"))
        if not city:
            continue
        if way == "Курьер по городу" and addr:
            almaty.append(_clean_addr(addr))
        elif "алмат" not in city.lower():
            key = re.sub(r"\s+", " ", city).strip().title()
            regions[key] = regions.get(key, 0) + 1
    res["orders"] = len(orders)
    # ---------- Яндекс: Алматы ----------
    ya = c["svc"]["yandex"]
    if ya.get("token"):
        wh = store.get("wh_addr") or ""
        src = geocode(wh if "алмат" in wh.lower() else f"{wh}, Алматы") if wh else None
        res["from"] = wh if src else "центр Алматы (адрес склада не найден на карте)"
        src = src or ALMATY
        pts = []
        for a in list(dict.fromkeys(almaty))[:MAX_POINTS]:
            p = geocode(f"{a}, Алматы")
            if p:
                pts.append((a, p))
        res["almaty_addresses"] = [len(set(almaty)), len(pts)]
        for label, kg in PROFILES:
            vals = []
            for a, p in pts:
                try:
                    v, cur = yandex_price(ya["token"], src, p, kg, box)
                    vals.append(v)
                    res["yandex_currency"] = cur
                except Exception as e:
                    res["errors"].append(f"Яндекс «{a}»: {str(e)[:120]}")
            res["yandex"][label] = _stats(vals)
    else:
        res["errors"].append("Яндекс: нет ключа")
    # ---------- СДЭК: регионы ----------
    cd = c["svc"]["cdek"]
    if cd.get("client_id") and cd.get("secret"):
        try:
            cdek = delivery.Cdek(cd)
            tok = cdek.token()
            src = cdek_city(cdek, tok, "Алматы")
            codes = []
            for name, cnt in sorted(regions.items(), key=lambda x: -x[1])[:MAX_POINTS]:
                try:
                    code = cdek_city(cdek, tok, name)
                    if code:
                        codes.append((name, cnt, code))
                    else:
                        res["errors"].append(f"СДЭК: город «{name}» не найден")
                except Exception as e:
                    res["errors"].append(f"СДЭК «{name}»: {str(e)[:120]}")
            res["regions"] = [len(regions), len(codes)]
            for label, kg in PROFILES:
                pv, dv, pw, dw = [], [], [], []
                for name, cnt, code in codes:
                    try:
                        p, d, cur = cdek_prices(cdek, tok, src, code, kg, box)
                        res["cdek_currency"] = cur or res.get("cdek_currency", "")
                        if p is not None:
                            pv.append(p), pw.append(cnt)
                        if d is not None:
                            dv.append(d), dw.append(cnt)
                        if label == PROFILES[0][0] and len(res["samples"]) < 12:
                            res["samples"].append({"city": name, "orders": cnt, "pvz": p, "door": d})
                    except Exception as e:
                        res["errors"].append(f"СДЭК «{name}»: {str(e)[:120]}")
                res["cdek_pvz"][label], res["cdek_door"][label] = _stats(pv, pw), _stats(dv, dw)
        except Exception as e:
            res["errors"].append(f"СДЭК: {str(e)[:150]}")
    else:
        res["errors"].append("СДЭК: нет ключей")
    res["errors"] = res["errors"][:30]
    with inbox.db() as d:
        inbox.set_setting(d, "dlv_estimate", json.dumps(res, ensure_ascii=False))
    def fmt(s):
        return f"ср {s['avg']} мед {s['med']} p90 {s['p90']} ({s['n']} точек)" if s else "—"
    print("Доставка, расчёт по прошлым заказам: " + "; ".join(
        f"{t} {lbl}: {fmt(res[k].get(lbl))}" for k, t in (("yandex", "Яндекс Алматы"), ("cdek_pvz", "СДЭК пункт"), ("cdek_door", "СДЭК дверь"))
        for lbl, _ in PROFILES) + f"; ошибок {len(res['errors'])}" + (f" (первая: {res['errors'][0]})" if res["errors"] else ""), flush=True)
    return res


def run_bg():
    def go():
        if not _lock.acquire(blocking=False):
            return
        try:
            run()
        except Exception as e:
            print("Доставка, расчёт: ошибка", str(e)[:200], flush=True)
            with inbox.db() as d:
                inbox.set_setting(d, "dlv_estimate", json.dumps({"at": time.time(), "fatal": str(e)[:300]}, ensure_ascii=False))
        finally:
            _lock.release()
    if _lock.locked():
        return False
    threading.Thread(target=go, daemon=True).start()
    return True


def last():
    with inbox.db() as d:
        raw = inbox.get_setting(d, "dlv_estimate", "")
    try:
        r = json.loads(raw) if raw else None
    except ValueError:
        r = None
    return {"running": _lock.locked(), "result": r}


def boot():
    """После выкладки: если расчёта ещё не было, а ключи есть — посчитать один раз."""
    try:
        c = delivery.conf()["svc"]
        if not last()["result"] and (c["yandex"].get("token") or c["cdek"].get("secret")):
            run_bg()
    except Exception as e:
        print("Доставка, расчёт: автозапуск не удался:", e, flush=True)
