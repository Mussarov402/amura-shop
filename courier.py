"""Курьер по Алматы через Яндекс Доставку «В течение дня»: забирают со склада пачкой и развозят за ~4 часа.

Панель → Заказы → «Курьер Яндекс»: розничные заказы «Курьер по Алматы» за выбранный день, адрес каждого — на карте
(координаты ищем сами, можно поправить вручную). Менеджер выбирает интервал забора из тех, что дал Яндекс, и нажимает
«Рассчитать» — по каждому заказу создаётся заявка в Яндексе (только оценка, ничего не стоит). «Подтвердить» — заявки
принимаются, курьер едет. Без нажатия в панели сервер ничего платного не вызывает.
Яндекс не присылает статусы сам (только опрос) — панель обновляет их кнопкой «Обновить» и при открытии раздела.
"""
import json
import re
import time
import uuid
from datetime import datetime, timedelta

import requests
from flask import Blueprint, jsonify, request

import delivery
import inbox
import order_hook as oh
from admin import need

bp = Blueprint("courier", __name__)
TIMEOUT = 20
YA = delivery.Yandex.BASE + "/b2b/cargo/integration/v2"
SHIP_MARK = "Курьер по Алматы"
# Алматы: рамка для поиска адреса (запад, север, восток, юг)
ALMATY_BOX = (76.70, 43.40, 77.20, 43.10)
STATUS = {"new": "Ещё не рассчитан", "estimating": "Яндекс считает цену…", "ready_for_approval": "Рассчитан — ждёт подтверждения",
          "accepted": "Подтверждён", "performer_lookup": "Ищем курьера", "performer_draft": "Ищем курьера",
          "performer_found": "Курьер назначен", "pickup_arrived": "Курьер на складе", "ready_for_pickup_confirmation": "Курьер на складе",
          "pickuped": "Забрали со склада", "delivery_arrived": "Курьер у клиента", "pay_waiting": "Курьер у клиента",
          "ready_for_delivery_confirmation": "Курьер у клиента", "delivered": "Доставлен", "delivered_finish": "Доставлен",
          "returning": "Возвращается на склад", "return_arrived": "Возвращается на склад", "returned": "Возвращён на склад",
          "returned_finish": "Возвращён на склад", "cancelled": "Отменён", "cancelled_with_payment": "Отменён (платно)",
          "cancelled_by_taxi": "Отменён Яндексом", "cancelled_with_items_on_hands": "Отменён, товар у курьера",
          "failed": "Ошибка у Яндекса", "estimating_failed": "Яндекс не смог рассчитать"}
DONE = {"delivered", "delivered_finish", "returned", "returned_finish", "cancelled", "cancelled_with_payment",
        "cancelled_by_taxi", "failed", "estimating_failed"}
_ready = [False]


def db():
    d = inbox.db()
    if not _ready[0]:
        d.run("CREATE TABLE IF NOT EXISTS ya_claim (num TEXT PRIMARY KEY, order_id TEXT, claim_id TEXT, status TEXT, version INTEGER,"
              " price DOUBLE PRECISION, ifrom TEXT, ito TEXT, lon DOUBLE PRECISION, lat DOUBLE PRECISION, addr TEXT, err TEXT,"
              " at DOUBLE PRECISION, updated DOUBLE PRECISION)")
        d.c.commit()
        for col in ("agent_id TEXT", "track TEXT"):          # чей заказ (клиент видит только свои) и ссылка «где курьер»
            try:
                d.run("ALTER TABLE ya_claim ADD COLUMN " + col)
                d.c.commit()
            except Exception:
                d.c.rollback()
        _ready[0] = True
    return d


# ---------- Яндекс API ----------
def _token():
    c = delivery.conf()["svc"]["yandex"]
    if not c.get("token"):
        raise RuntimeError("Ключ Яндекс Доставки не введён — Обзор → Доставка → Яндекс")
    return c["token"]


def ya(path, body=None, **params):
    r = requests.post(YA + path, params=params or None, json=body or {}, timeout=TIMEOUT,
                      headers={"Authorization": "Bearer " + _token(), "Accept-Language": "ru"})
    if r.status_code >= 400:
        try:
            msg = r.json().get("message") or r.text
        except Exception:
            msg = r.text
        raise RuntimeError(f"Яндекс: {str(msg)[:240]}")
    return r.json()


# ---------- координаты ----------
def parse_coords(s):
    return delivery.parse_coords(s)


def geocode(address):
    """Адрес в Алматы → (lon, lat) по OpenStreetMap; None — не нашли (тогда координаты вводят в панели)."""
    q = re.sub(r"\b(кв|квартира|офис|под[ъь]езд|этаж|подъезд)\.?\s*\d+\w*", "", str(address), flags=re.I).strip(" ,")
    def find():
        r = requests.get("https://nominatim.openstreetmap.org/search", timeout=TIMEOUT,
                         params={"q": f"{q}, Алматы", "format": "json", "limit": 1, "countrycodes": "kz",
                                 "viewbox": ",".join(map(str, ALMATY_BOX)), "bounded": 1},
                         headers={"User-Agent": "amura.kz shop (admin@amura.kz)", "Accept-Language": "ru"})
        r.raise_for_status()
        j = r.json()
        return [float(j[0]["lon"]), float(j[0]["lat"])] if j else []
    try:
        res = oh.cached("geo:" + q.lower(), 86400 * 7, find)
    except Exception as e:
        print("Курьер: поиск адреса", e, flush=True)
        return None
    return tuple(res) if res else None


def warehouse():
    st = delivery.conf()["store"]
    pt = delivery.resolve_coords(st.get("wh_coords")) or geocode(st.get("wh_addr", ""))
    if not pt:
        raise RuntimeError("Не найдены координаты склада — Обзор → Доставка → «Координаты склада»")
    return {"addr": st.get("wh_addr", ""), "phone": re.sub(r"\D", "", st.get("wh_phone", "")), "pt": pt}


# ---------- заказы ----------
def parse_order(o):
    """Из описания заказа сайта: имя, телефон, адрес и выбранный клиентом интервал."""
    lines = (o.get("description") or "").split("\n")
    who = lines[1] if len(lines) > 1 else ""
    ship = next((l[len("Отправка: "):] for l in lines if l.startswith("Отправка: ")), "")
    name = who.split(",")[0].strip() or (o.get("agent") or {}).get("name", "")
    m = re.search(r"\+?(\d{10,12})", who)
    phone = m.group(1) if m else re.sub(r"\D", "", (o.get("agent") or {}).get("phone", ""))
    addr, slot = ship.split(" — ", 1)[1] if " — " in ship else "", ""
    m = re.search(r",\s*(\d\d\.\d\d\s.*)$", addr)
    if m:
        addr, slot = addr[:m.start()].strip(), m.group(1).strip()
    return {"id": o["id"], "num": o["name"], "name": name, "phone": phone, "addr": addr, "slot": slot,
            "agent": (o.get("agent") or {}).get("id") or str((o.get("agent") or {}).get("meta", {}).get("href", "")).rsplit("/", 1)[-1],
            "sum": round(o.get("sum", 0) / 100), "state": (o.get("state") or {}).get("name", ""), "moment": o.get("moment", "")[:16]}


def day_orders(day):
    """Розничные заказы с курьером по Алматы: созданные за день до выбранного и в сам день (МойСклад — московское время)."""
    d0 = datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)
    a = (d0 - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")             # Алматы UTC+5 → Москва UTC+3
    b = (d0 + timedelta(days=2) - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    r = oh.ms("GET", "/entity/customerorder", params={"filter": f"description~{SHIP_MARK};moment>={a};moment<{b}",
                                                      "order": "moment,asc", "limit": 100, "expand": "agent,state"}, timeout=20)
    return [parse_order(o) for o in r.get("rows", []) if (o.get("description") or "").startswith(oh.RETAIL_MARK)
            and "Яндекс Экспресс" not in (o.get("description") or "")]      # срочные — отдельно, не «в течение дня»


def _row(d, num):
    r = d.run("SELECT claim_id, status, version, price, ifrom, ito, lon, lat, addr, err FROM ya_claim WHERE num=%s", (num,), one=True)
    if not r:
        return None
    return {"claim": r[0], "status": r[1], "version": r[2], "price": r[3], "from": r[4], "to": r[5],
            "pt": [r[6], r[7]] if r[6] is not None else None, "addr": r[8], "err": r[9] or ""}


def _save(d, num, **kw):
    cur = d.run("SELECT 1 FROM ya_claim WHERE num=%s", (num,), one=True)
    kw["updated"] = time.time()
    if cur:
        d.run("UPDATE ya_claim SET " + ", ".join(f"{k}=%s" for k in kw) + " WHERE num=%s", (*kw.values(), num))
    else:
        kw.setdefault("at", time.time())
        d.run("INSERT INTO ya_claim (num, " + ", ".join(kw) + ") VALUES (%s, " + ", ".join(["%s"] * len(kw)) + ")", (num, *kw.values()))


def _price(info):
    try:
        return float(((info.get("pricing") or {}).get("offer") or {}).get("price") or (info.get("pricing") or {}).get("final_price") or 0) or None
    except (TypeError, ValueError):
        return None


def claim_body(o, pt, wh, interval):
    rules = delivery.conf()["rules"]
    w = max(0.1, round(int(rules.get("weight") or 300) * max(1, o.get("qty", 1)) / 1000, 2))
    size = {"length": rules["box_d"] / 100, "width": rules["box_w"] / 100, "height": rules["box_h"] / 100}
    return {
        "items": [{"title": f"Заказ №{o['num']} (косметика)", "quantity": 1, "cost_value": str(o["sum"]), "cost_currency": "KZT",
                   "weight": w, "size": size, "pickup_point": 1, "droppof_point": 2}],
        "route_points": [
            {"point_id": 1, "visit_order": 1, "type": "source", "skip_confirmation": True,
             "contact": {"name": "AMURA", "phone": "+" + wh["phone"]},
             "address": {"fullname": "Алматы, " + wh["addr"], "coordinates": list(wh["pt"])}},
            {"point_id": 2, "visit_order": 2, "type": "destination", "skip_confirmation": True,
             "contact": {"name": o["name"] or "Клиент", "phone": "+" + o["phone"]},
             "address": {"fullname": "Алматы, " + o["addr"], "coordinates": list(pt)},
             "external_order_id": o["num"]},
        ],
        "same_day_data": {"delivery_interval": {"from": interval["from"], "to": interval["to"]}},
        "comment": f"AMURA, заказ №{o['num']}. Позвонить клиенту за 15 минут.",
        "emergency_contact": {"name": "AMURA", "phone": "+" + wh["phone"]},
        "optional_return": False,
    }


# ---------- API панели ----------
@bp.get("/admin/api/courier")
@need("orders")
def courier_day():
    day = request.args.get("date") or datetime.now(oh.ALMATY).strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", day):
        return jsonify(ok=False, error="Неверная дата"), 400
    ready = bool(delivery.conf()["svc"]["yandex"].get("token"))
    try:
        orders = day_orders(day)
    except Exception as e:
        return jsonify(ok=False, error=f"МойСклад: {str(e)[:200]}"), 503
    intervals, err = [], ""
    if ready:
        try:
            wh = warehouse()
            m = oh.cached(f"ya_methods:{day}", 300, lambda: ya("/delivery-methods", {"start_point": list(wh["pt"]), "fullname": "Алматы, " + wh["addr"]}))
            sd = m.get("same_day_delivery") or {}
            if sd.get("allowed") is False:
                err = "Яндекс: «В течение дня» для этого адреса склада недоступна"
            intervals = [{"from": i["from"], "to": i["to"]} for i in sd.get("available_intervals") or [] if i.get("from") and i.get("to")]
        except Exception as e:
            err = str(e)[:300]
    with db() as d:
        for o in orders:
            o["ya"] = _row(d, o["num"])
            if not (o["ya"] and o["ya"]["pt"]) and o["addr"]:
                pt = geocode(o["addr"])
                if pt:
                    _save(d, o["num"], order_id=o["id"], status=(o["ya"] or {}).get("status") or "new", lon=pt[0], lat=pt[1], addr=o["addr"])
                    o["ya"] = _row(d, o["num"])
            o["statusText"] = STATUS.get((o["ya"] or {}).get("status") or "new", (o["ya"] or {}).get("status") or "")
    windows = [s.get("from", "") for s in (delivery.conf().get("schedule") or {}).get("slots", [])]     # окна, которые выбирают клиенты на сайте
    return jsonify(ok=True, date=day, ready=ready, error=err, intervals=intervals, windows=windows, orders=orders)


@bp.post("/admin/api/courier/coords")
@need("orders")
def courier_coords():
    b = request.get_json(silent=True) or {}
    pt = delivery.resolve_coords(b.get("coords"))
    if not pt:
        return jsonify(ok=False, error="Не понял координаты — вставьте «43.23, 76.94» или ссылку из 2ГИС / Яндекс Карт"), 400
    with db() as d:
        _save(d, str(b.get("num", ""))[:20], lon=pt[0], lat=pt[1])
    return jsonify(ok=True, pt=list(pt))


@bp.post("/admin/api/courier/estimate")
@need("orders")
def courier_estimate():
    """Заявки в Яндексе по выбранным заказам — только расчёт цены (без подтверждения ничего не стоит)."""
    b = request.get_json(silent=True) or {}
    iv = b.get("interval") or {}
    if not (iv.get("from") and iv.get("to")):
        return jsonify(ok=False, error="Выберите интервал забора"), 400
    nums = [str(n)[:20] for n in (b.get("nums") or [])][:30]
    try:
        wh = warehouse()
        orders = {o["num"]: o for o in day_orders(b.get("date") or datetime.now(oh.ALMATY).strftime("%Y-%m-%d"))}
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 400
    res = {}
    with db() as d:
        for n in nums:
            o, row = orders.get(n), _row(d, n)
            if not o:
                res[n] = "заказ не найден за этот день"
                continue
            if row and row["claim"] and row["status"] not in DONE:
                res[n] = "уже есть заявка"
                continue
            if not o["phone"]:
                res[n] = "нет телефона клиента"
                continue
            pt = (row or {}).get("pt") or geocode(o["addr"])
            if not pt:
                res[n] = "не найден адрес — укажите координаты"
                continue
            try:
                j = ya("/claims/create", claim_body(o, pt, wh, iv), request_id=str(uuid.uuid4()))
                _save(d, n, order_id=o["id"], agent_id=o.get("agent", ""), claim_id=j.get("id"), status=j.get("status", "estimating"), version=j.get("version", 1),
                      ifrom=iv["from"], ito=iv["to"], lon=pt[0], lat=pt[1], addr=o["addr"], err="", price=_price(j))
                res[n] = "ok"
            except Exception as e:
                _save(d, n, order_id=o["id"], err=str(e)[:300], status="new")
                res[n] = str(e)[:200]
    return jsonify(ok=True, result=res)


def refresh(nums=None):
    with db() as d:
        rows = d.run("SELECT num, claim_id FROM ya_claim WHERE claim_id IS NOT NULL AND claim_id<>''", many=True) or []
        for num, cid in rows:
            if nums is not None and num not in nums:
                continue
            row = _row(d, num)
            if row["status"] in DONE:
                continue
            try:
                j = ya("/claims/info", claim_id=cid)
                st = j.get("status", row["status"])
                _save(d, num, status=st, version=j.get("version", row["version"]),
                      price=_price(j) or row["price"], err=(j.get("error_messages") or [{}])[0].get("message", "") if j.get("error_messages") else "")
                if st in TRACKABLE and not d.run("SELECT track FROM ya_claim WHERE num=%s", (num,), one=True)[0]:
                    link = tracking_link(cid)
                    if link:
                        _save(d, num, track=link)
            except Exception as e:
                _save(d, num, err=str(e)[:300])


# ---------- отслеживание для покупателя (сайт → «Мои заказы») ----------
TRACKABLE = {"performer_found", "pickup_arrived", "ready_for_pickup_confirmation", "pickuped", "delivery_arrived",
             "pay_waiting", "ready_for_delivery_confirmation"}
# шаг ленты (0 принят · 1 курьер назначен · 2 в пути · 3 доставлен) и текст для клиента
CLIENT = {"accepted": (0, "Ищем курьера"), "performer_lookup": (0, "Ищем курьера"), "performer_draft": (0, "Ищем курьера"),
          "performer_found": (1, "Курьер назначен"), "pickup_arrived": (1, "Курьер приехал за заказом"),
          "ready_for_pickup_confirmation": (1, "Курьер приехал за заказом"), "pickuped": (2, "Курьер забрал заказ и едет к вам"),
          "delivery_arrived": (2, "Курьер у вас"), "pay_waiting": (2, "Курьер у вас"), "ready_for_delivery_confirmation": (2, "Курьер у вас"),
          "delivered": (3, "Доставлен"), "delivered_finish": (3, "Доставлен"),
          "returning": (-1, "Возвращается на склад — свяжемся с вами"), "returned": (-1, "Вернулся на склад — свяжемся с вами"),
          "returned_finish": (-1, "Вернулся на склад — свяжемся с вами"), "cancelled": (-1, "Доставка отменена — свяжемся с вами"),
          "cancelled_by_taxi": (-1, "Доставка отменена — свяжемся с вами"), "cancelled_with_payment": (-1, "Доставка отменена — свяжемся с вами"),
          "cancelled_with_items_on_hands": (-1, "Доставка отменена — свяжемся с вами"), "failed": (-1, "Задержка доставки — свяжемся с вами")}


def tracking_link(cid):
    """Ссылка «где курьер» (карта Яндекса) для получателя; None — Яндекс не дал."""
    try:
        j = ya("/claims/tracking-links", claim_id=cid)
    except Exception as e:
        print("Курьер: ссылка отслеживания", str(e)[:200], flush=True)
        return None
    def find(x):
        if isinstance(x, dict):
            if x.get("type") == "destination" and isinstance(x.get("sharing_link"), str):
                return x["sharing_link"]
            for v in x.values():
                r = find(v)
                if r:
                    return r
        if isinstance(x, list):
            for v in x:
                r = find(v)
                if r:
                    return r
        if isinstance(x, str) and x.startswith("https://") and "track" in x.lower():
            return x
        return None
    return find(j)


def client_tracks(agent_id, nums=None):
    """{номер заказа: отслеживание} — только заказы этого покупателя, только с подтверждённой заявкой Яндекса."""
    with db() as d:
        rows = d.run("SELECT num, status, ifrom, ito, track FROM ya_claim WHERE agent_id=%s AND claim_id IS NOT NULL AND claim_id<>''",
                     (agent_id,), many=True) or []
    out = {}
    for num, st, ifrom, ito, track in rows:
        if nums is not None and num not in nums:
            continue
        step, label = CLIENT.get(st, (None, None))
        if step is None:
            continue                                       # ещё не подтверждено (оценка) — клиенту не показываем
        out[num] = {"code": st, "step": step, "label": label, "link": track if step in (1, 2) else "",
                    "from": ifrom or "", "to": ito or "", "kind": "yandex"}
    return out


def _poll():
    """Фон: раз в 2,5 минуты обновляем статусы подтверждённых заявок (Яндекс сам их не присылает)."""
    while True:
        time.sleep(150)
        try:
            if delivery.conf()["svc"]["yandex"].get("token"):
                refresh()
        except Exception as e:
            print("Курьер: фоновое обновление", str(e)[:200], flush=True)


_poller = [None]


def start_poller():
    import threading
    if _poller[0] is None:
        _poller[0] = threading.Thread(target=_poll, daemon=True, name="ya-poll")
        _poller[0].start()


@bp.post("/admin/api/courier/refresh")
@need("orders")
def courier_refresh():
    b = request.get_json(silent=True) or {}
    try:
        refresh([str(n) for n in b["nums"]] if b.get("nums") else None)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 400
    return jsonify(ok=True)


@bp.post("/admin/api/courier/accept")
@need("orders")
def courier_accept():
    """Подтвердить рассчитанные заявки — после этого Яндекс ищет курьера (это уже платно)."""
    nums = [str(n)[:20] for n in ((request.get_json(silent=True) or {}).get("nums") or [])][:30]
    refresh(nums)
    res = {}
    with db() as d:
        for n in nums:
            row = _row(d, n)
            if not row or not row["claim"]:
                res[n] = "сначала рассчитайте"
                continue
            if row["status"] != "ready_for_approval":
                res[n] = STATUS.get(row["status"], row["status"])
                continue
            try:
                j = ya("/claims/accept", {"version": row["version"] or 1}, claim_id=row["claim"])
                _save(d, n, status=j.get("status", "accepted"), version=j.get("version", row["version"]), err="")
                res[n] = "ok"
            except Exception as e:
                _save(d, n, err=str(e)[:300])
                res[n] = str(e)[:200]
    return jsonify(ok=True, result=res)


@bp.post("/admin/api/courier/cancel")
@need("orders")
def courier_cancel():
    n = str((request.get_json(silent=True) or {}).get("num", ""))[:20]
    with db() as d:
        row = _row(d, n)
        if not row or not row["claim"]:
            return jsonify(ok=False, error="Заявки нет"), 404
        try:
            info = ya("/claims/info", claim_id=row["claim"])
            state = info.get("available_cancel_state") or "free"
            if state not in ("free", "paid"):
                return jsonify(ok=False, error="Яндекс уже не даёт отменить эту заявку"), 400
            ya("/claims/cancel", {"version": info.get("version", row["version"]), "cancel_state": state}, claim_id=row["claim"])
            _save(d, n, status="cancelled", err="")
        except Exception as e:
            return jsonify(ok=False, error=str(e)[:300]), 400
    return jsonify(ok=True, paid=state == "paid")
