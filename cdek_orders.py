"""СДЭК: отправления из системы AMURA (Хиро → Система, 20.11.2026), по образцу вкладки «Курьер».

Заказы → «СДЭК»: заказы с «Отправка: Пункт выдачи СДЭК — … (код XXX)» — Новые → Собраны → В пути → Доставлены.
В «Собраны»: «Рассчитать» (бесплатно, тариф СДЭК по весу) и «Создать отправление» (платно — только по кнопке и после окна
подтверждения на странице): POST /v2/orders — тариф до пункта выдачи (или постамата), отправитель — склад AMURA из «Доставки»,
получатель — ФИО и телефон из заказа, код ПВЗ из заказа, вес из карточек МС (пусто — вес по умолчанию), коробка по умолчанию,
объявленная ценность = сумма товаров, наложенного платежа нет (всё оплачено).
После создания номер СДЭК пишется в описание заказа МС строкой «Трек СДЭК: …» (её берёт адресная этикетка).
Накладная и наклейки — PDF (/v2/print/orders, /v2/print/barcodes). Курьер СДЭК на забор — /v2/intakes на выбранный день.
Статусы — GET /v2/orders/{uuid} по кнопке и в фоне раз в 30 минут; «Вручен» → в МС «Доставлен»; фактическая стоимость —
в «Экономику заказа»; покупателю в «Моих заказах» — трек и ссылка на cdek.kz. Тестовый режим СДЭК — api.edu.cdek.ru (delivery.Cdek)."""
import json
import re
import threading
import time
from datetime import datetime, timedelta

import requests
from flask import Blueprint, Response, jsonify, request

import delivery
import inbox
import order_hook as oh
from admin import need

bp = Blueprint("cdek_orders", __name__)
TIMEOUT = 25
MARK = "Пункт выдачи СДЭК"
DAYS = 14
# тарифы СДЭК: откуда (door — курьер СДЭК забирает со склада, office — сдаём в пункт приёма) × куда (pvz / postamat)
TARIFFS = {("door", "pvz"): 138, ("door", "postamat"): 366, ("office", "pvz"): 136, ("office", "postamat"): 368}
SET = {"from": "cdek_from_mode", "office": "cdek_from_office", "t_door_pvz": "cdek_t_door_pvz", "t_door_post": "cdek_t_door_post",
       "t_office_pvz": "cdek_t_office_pvz", "t_office_post": "cdek_t_office_post"}
STATUS = {"CREATED": "Создано", "ACCEPTED": "Принято СДЭК", "INVALID": "Ошибка в отправлении",
          "RECEIVED_AT_SHIPMENT_WAREHOUSE": "Принято на склад СДЭК", "READY_TO_SHIP_AT_SENDING_OFFICE": "Готово к отправке",
          "READY_FOR_SHIPMENT_IN_TRANSIT_CITY": "В пути", "TAKEN_BY_TRANSPORTER_FROM_SENDER_CITY": "В пути",
          "SENT_TO_TRANSIT_CITY": "В пути", "ACCEPTED_IN_TRANSIT_CITY": "В пути", "SENT_TO_RECIPIENT_CITY": "В пути",
          "ACCEPTED_IN_RECIPIENT_CITY": "Прибыло в город получателя", "ACCEPTED_AT_RECIPIENT_CITY_WAREHOUSE": "Прибыло в город получателя",
          "ACCEPTED_AT_PICK_UP_POINT": "Ждёт в пункте выдачи", "POSTOMAT_POSTED": "Ждёт в постамате",
          "TAKEN_BY_COURIER": "У курьера", "DELIVERED": "Вручено", "NOT_DELIVERED": "Не вручено", "POSTOMAT_RECEIVED": "Вручено",
          "RETURNED_TO_SENDER_CITY_WAREHOUSE": "Возвращается", "RETURNED": "Возвращено"}
NOT_SENT = {"", "CREATED", "ACCEPTED", "INVALID"}             # отправление есть, но СДЭК посылку ещё не принял — этап «Собраны»
DONE = {"DELIVERED", "POSTOMAT_RECEIVED", "NOT_DELIVERED", "RETURNED", "CANCELLED"}
TRACK_URL = "https://www.cdek.kz/ru/tracking?order_id="
_ready = [False]


def db():
    d = inbox.db()
    if not _ready[0]:
        d.run("CREATE TABLE IF NOT EXISTS cdek_ship (num TEXT PRIMARY KEY, order_id TEXT, agent_id TEXT, uuid TEXT, cdek_number TEXT,"
              " status TEXT, tariff INTEGER, price DOUBLE PRECISION, est DOUBLE PRECISION, err TEXT, track_written INTEGER DEFAULT 0,"
              " created DOUBLE PRECISION, updated DOUBLE PRECISION)")
        d.c.commit()
        _ready[0] = True
    return d


def row(d, num):
    r = d.run("SELECT num, order_id, agent_id, uuid, cdek_number, status, tariff, price, est, err, track_written FROM cdek_ship WHERE num=%s",
              (num,), one=True)
    if not r:
        return None
    return dict(zip(("num", "order_id", "agent_id", "uuid", "number", "status", "tariff", "price", "est", "err", "trackWritten"), r))


def save(d, num, **kw):
    kw["updated"] = time.time()
    if d.run("SELECT 1 FROM cdek_ship WHERE num=%s", (num,), one=True):
        d.run("UPDATE cdek_ship SET " + ", ".join(f"{k}=%s" for k in kw) + " WHERE num=%s", (*kw.values(), num))
    else:
        kw.setdefault("created", time.time())
        d.run("INSERT INTO cdek_ship (num, " + ", ".join(kw) + ") VALUES (%s, " + ", ".join(["%s"] * len(kw)) + ")", (num, *kw.values()))


# ---------- настройки отправки ----------
def settings():
    with inbox.db() as d:
        g = {k: inbox.get_setting(d, v, "") for k, v in SET.items()}
    return {"from": g["from"] if g["from"] in ("door", "office") else "door", "office": g["office"],
            "tariffs": {("door", "pvz"): int(g["t_door_pvz"] or TARIFFS[("door", "pvz")]),
                        ("door", "postamat"): int(g["t_door_post"] or TARIFFS[("door", "postamat")]),
                        ("office", "pvz"): int(g["t_office_pvz"] or TARIFFS[("office", "pvz")]),
                        ("office", "postamat"): int(g["t_office_post"] or TARIFFS[("office", "postamat")])}}


def public_settings():
    s = settings()
    return {"from": s["from"], "office": s["office"], **{f"t_{a}_{'post' if b == 'postamat' else b}": v for (a, b), v in s["tariffs"].items()}}


def save_settings(b):
    with inbox.db() as d:
        if b.get("from") in ("door", "office"):
            inbox.set_setting(d, SET["from"], b["from"])
        if "office" in b:
            inbox.set_setting(d, SET["office"], re.sub(r"[^\w-]", "", str(b["office"] or ""))[:20])
        for k in ("t_door_pvz", "t_door_post", "t_office_pvz", "t_office_post"):
            if str(b.get(k, "")).strip():
                v = int(b[k])
                if not 1 <= v <= 9999:
                    raise ValueError("Код тарифа СДЭК — число")
                inbox.set_setting(d, SET[k], str(v))
    return public_settings()


# ---------- СДЭК API ----------
def _cd():
    c = delivery.conf()["svc"]["cdek"]
    if not (c.get("client_id") and c.get("secret")):
        raise RuntimeError("Ключи СДЭК не введены: Настройки → Доставка → СДЭК")
    return delivery.Cdek(c)


def api(method, path, body=None, params=None, raw=False):
    cd = _cd()
    key = "cdek_tok_test" if cd.test_mode else "cdek_tok"
    tok = oh.cached(key, 3000, lambda: cd.token())
    r = requests.request(method, cd.base + path, json=body, params=params, timeout=TIMEOUT,
                         headers={"Authorization": "Bearer " + tok, "Accept-Language": "rus"})
    if raw:
        r.raise_for_status()
        return r
    try:
        j = r.json()
    except ValueError:
        j = {}
    if r.status_code >= 400:
        errs = []
        if isinstance(j, dict):
            errs = [e.get("message") for q in j.get("requests") or [] for e in q.get("errors") or []] + \
                   [e.get("message") for e in j.get("errors") or []]
        raise RuntimeError(f"СДЭК {r.status_code}: " + ("; ".join(x for x in errs if x) or r.text[:200]))
    return j


def point(code):
    """Пункт выдачи по коду → {type: pvz|postamat, city_code, address}. Кэш сутки."""
    def load():
        j = api("GET", "/deliverypoints", params={"code": code})
        p = (j or [{}])[0] if isinstance(j, list) else {}
        loc = p.get("location") or {}
        return {"type": "postamat" if (p.get("type") or "").upper() == "POSTAMAT" else "pvz", "city_code": loc.get("city_code"),
                "address": loc.get("address_full") or loc.get("address") or ""}
    return oh.cached("cdek_pt:" + code, 86400, load)


def city_code(city):
    def load():
        j = api("GET", "/location/cities", params={"country_codes": "KZ", "city": city, "size": 1})
        return (j or [{}])[0].get("code") if isinstance(j, list) and j else None
    return oh.cached("cdek_city2:" + city.lower(), 86400, load)


# ---------- заказы ----------
def parse(o):
    """Заказ МС → поля отправления."""
    import courier
    x = courier.parse_order(o)
    desc = o.get("description") or ""
    ship = next((l[len("Отправка: "):] for l in desc.split("\n") if l.startswith("Отправка: ")), "")
    m = re.search(r"\(код ([\w-]+)\)", ship)
    city = next((l[len("Город: "):].strip() for l in desc.split("\n") if l.startswith("Город: ")), "")
    return {"id": o["id"], "num": str(o.get("name") or ""), "name": x["name"], "phone": x["phone"], "agent": x["agent"],
            "pvz": m.group(1) if m else "", "pvzAddr": ship.split(" — ", 1)[1].split(" (код")[0] if " — " in ship else "",
            "city": city, "sum": round((o.get("sum") or 0) / 100), "paid": round((o.get("payedSum") or 0) / 100),
            "state": ((o.get("state") or {}).get("name")) or "", "moment": (o.get("moment") or "")[:16],
            "retail": desc.startswith(oh.RETAIL_MARK)}


def orders(days=DAYS):
    since = (datetime.now(oh.ALMATY) - timedelta(days=days, hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for off in range(0, 500, 100):
        j = oh.ms("GET", "/entity/customerorder", params={"filter": f"description~{MARK};moment>={since}", "order": "moment,desc",
                                                           "limit": 100, "offset": off, "expand": "agent,state"}, timeout=25)
        rows = j.get("rows") or []
        out += [parse(o) for o in rows if f"Отправка: {MARK}" in (o.get("description") or "")]
        if len(rows) < 100:
            break
    return out


def _goods(order_id):
    """Позиции-товары с весом из карточки МС: [(код, название, кол-во, цена ₸, вес г)]."""
    rules = delivery.conf()["rules"]
    j = oh.ms("GET", f"/entity/customerorder/{order_id}/positions", params={"expand": "assortment", "limit": 1000}, timeout=25)
    out = []
    for p in j.get("rows") or []:
        a = p.get("assortment") or {}
        if ((a.get("meta") or {}).get("type")) == "service":
            continue
        w = int(round((a.get("weight") or 0) * 1000)) or int(rules.get("weight") or 300)
        out.append((a.get("code") or a.get("id", "")[:20], (a.get("name") or "Товар")[:250], int(p.get("quantity") or 0),
                    round((p.get("price") or 0) / 100), w))
    return out


def package(goods, rules=None):
    rules = rules or delivery.conf()["rules"]
    weight = max(100, sum(q * w for _, _, q, _, w in goods))
    return {"number": "1", "weight": weight, "length": int(rules["box_d"]), "width": int(rules["box_w"]), "height": int(rules["box_h"]),
            "items": [{"ware_key": str(c)[:50], "name": n, "cost": price, "weight": w, "amount": q, "payment": {"value": 0}}
                      for c, n, q, price, w in goods if q > 0]}


def tariff_for(pt, st=None):
    st = st or settings()
    return st["tariffs"][(st["from"], pt["type"])]


def body(x, goods, pt, st=None, store=None):
    """Тело POST /v2/orders: тип «интернет-магазин», без наложенного платежа."""
    st, store = st or settings(), store or delivery.conf()["store"]
    rules = delivery.conf()["rules"]
    phone = re.sub(r"\D", "", store.get("wh_phone") or "")
    b = {"type": 1, "number": f"AMURA-{x['num']}", "tariff_code": tariff_for(pt, st),
         "comment": f"AMURA, заказ №{x['num']}. Оплачено, наложенного платежа нет.",
         "delivery_point": x["pvz"], "delivery_recipient_cost": {"value": 0},
         "sender": {"company": "AMURA", "name": "AMURA", "phones": [{"number": "+" + phone}] if phone else []},
         "recipient": {"name": x["name"] or "Покупатель", "phones": [{"number": "+" + x["phone"]}]},
         "packages": [package(goods, rules)]}
    if st["from"] == "office":
        b["shipment_point"] = st["office"]
    else:
        b["from_location"] = {"code": city_code("Алматы"), "address": store.get("wh_addr") or "Алматы"}
    return b


def estimate(x, goods, pt, st=None):
    """Цена тарифа до создания (бесплатно): /v2/calculator/tariff."""
    st = st or settings()
    pk = package(goods)
    j = api("POST", "/calculator/tariff", {"tariff_code": tariff_for(pt, st), "from_location": {"code": city_code("Алматы")},
                                           "to_location": {"code": pt["city_code"]},
                                           "packages": [{k: pk[k] for k in ("weight", "length", "width", "height")}]})
    return float(j.get("total_sum") or j.get("delivery_sum") or 0)


def write_track(order_id, number):
    """«Трек СДЭК: …» в описание заказа МС (одна строка — заменяется, если была)."""
    o = oh.ms("GET", f"/entity/customerorder/{order_id}", timeout=20)
    desc = o.get("description") or ""
    line = f"Трек СДЭК: {number}"
    if line in desc:
        return False
    lines = [l for l in desc.split("\n") if not l.startswith("Трек СДЭК:")]
    i = next((k + 1 for k, l in enumerate(lines) if l.startswith("Отправка: ")), len(lines))
    lines.insert(i, line)
    oh.ms("PUT", f"/entity/customerorder/{order_id}", json={"description": "\n".join(lines)[:4000]}, timeout=20)
    return True


def refresh(nums=None):
    """Статусы и номера отправлений из СДЭК; номер — в заказ МС; «Вручено» → МС «Доставлен»; цена — в экономику."""
    with db() as d:
        rows = d.run("SELECT num FROM cdek_ship WHERE uuid IS NOT NULL AND uuid<>''", many=True) or []
        for (num,) in rows:
            if nums is not None and num not in nums:
                continue
            r = row(d, num)
            if r["status"] in DONE and r["trackWritten"]:
                continue
            try:
                e = (api("GET", f"/orders/{r['uuid']}") or {}).get("entity") or {}
            except Exception as ex:
                save(d, num, err=str(ex)[:300])
                continue
            sts = e.get("statuses") or []
            st = (sts[0].get("code") if sts else "") or r["status"] or ""
            number = e.get("cdek_number") or r["number"] or ""
            dd = e.get("delivery_detail") or {}
            price = dd.get("total_sum") or dd.get("delivery_sum") or r["price"]
            changed = st != r["status"] or (price or 0) != (r["price"] or 0)
            save(d, num, status=st, cdek_number=number, price=price, err="")
            if number and not r["trackWritten"] and r["order_id"]:
                try:
                    write_track(r["order_id"], number)
                    save(d, num, track_written=1)
                except Exception as ex:
                    print("СДЭК: трек в заказ не записан", num, str(ex)[:150], flush=True)
            if st in ("DELIVERED", "POSTOMAT_RECEIVED") and st != r["status"] and r["order_id"]:
                try:
                    import courier
                    courier.set_ms_state(r["order_id"], "Доставлен")
                except Exception as ex:
                    print("СДЭК: статус МС", num, str(ex)[:150], flush=True)
            if changed and r["order_id"]:
                try:
                    import retail_econ
                    retail_econ.write_bg(r["order_id"], delay=2)
                except Exception:
                    pass


def client_tracks(agent_id, nums=None):
    """Для «Моих заказов»: {номер: {step, label, link, kind}} — когда у отправления уже есть номер СДЭК."""
    with db() as d:
        rows = d.run("SELECT num, cdek_number, status FROM cdek_ship WHERE agent_id=%s AND cdek_number IS NOT NULL AND cdek_number<>''",
                     (agent_id,), many=True) or []
    out = {}
    for num, number, st in rows:
        if nums is not None and num not in nums:
            continue
        step = 3 if st in ("DELIVERED", "POSTOMAT_RECEIVED") else 2 if st not in NOT_SENT else 1
        out[num] = {"code": st or "CREATED", "step": step, "label": f"СДЭК: {STATUS.get(st, 'отправление создано')} · трек {number}",
                    "link": TRACK_URL + number, "from": "", "to": "", "kind": "cdek", "track": number}
    return out


def _poll():
    while True:
        time.sleep(1800)
        try:
            c = delivery.conf()["svc"]["cdek"]
            if c.get("client_id") and c.get("secret"):
                refresh()
        except Exception as e:
            print("СДЭК: фоновое обновление", str(e)[:200], flush=True)


_poller = [None]


def start_poller():
    import bg
    if bg.in_master():
        bg.start(start_poller)
        return
    if bg.STANDBY:
        return
    if _poller[0] is None:
        _poller[0] = threading.Thread(target=_poll, daemon=True, name="cdek-poll")
        _poller[0].start()


# ---------- API панели ----------
def _view(x, d):
    r = row(d, x["num"]) or {}
    x["cdek"] = {k: r.get(k) for k in ("uuid", "number", "status", "tariff", "price", "est", "err")} if r else None
    x["statusText"] = STATUS.get((r or {}).get("status") or "", "Отправление создано" if r.get("uuid") else "Ещё не создано") if r else "Ещё не создано"
    return x


@bp.get("/admin/api/cdek")
@need("orders")
def route_list():
    try:
        days = max(1, min(60, int(request.args.get("days") or DAYS)))
        rows = orders(days)
    except Exception as e:
        return jsonify(ok=False, error="МойСклад: " + str(e)[:200]), 503
    c = delivery.conf()["svc"]["cdek"]
    with db() as d:
        rows = [_view(x, d) for x in rows]
    return jsonify(ok=True, ready=bool(c.get("client_id") and c.get("secret")), test=c.get("test") == "1", orders=rows,
                   settings=public_settings(), store=delivery.conf()["store"].get("wh_addr") or "")


def _pick(nums):
    want = {str(n)[:20] for n in nums or []}
    return {x["num"]: x for x in orders(30) if x["num"] in want}


@bp.post("/admin/api/cdek/estimate")
@need("orders")
def route_estimate():
    """Только расчёт цены — в СДЭК ничего не создаётся."""
    nums = [str(n)[:20] for n in (request.get_json(silent=True) or {}).get("nums") or []][:30]
    res, st = {}, settings()
    try:
        got = _pick(nums)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 503
    with db() as d:
        for n in nums:
            x = got.get(n)
            if not x or not x["pvz"]:
                res[n] = {"error": "нет кода пункта выдачи в заказе" if x else "заказ не найден"}
                continue
            try:
                pt = point(x["pvz"])
                v = estimate(x, _goods(x["id"]), pt, st)
                save(d, n, order_id=x["id"], agent_id=x["agent"], est=v, tariff=tariff_for(pt, st))
                res[n] = {"est": round(v), "tariff": tariff_for(pt, st), "type": pt["type"]}
            except Exception as e:
                res[n] = {"error": str(e)[:200]}
    return jsonify(ok=True, result=res)


@bp.post("/admin/api/cdek/create")
@need("orders")
def route_create():
    """Создать отправления (платно). Только по кнопке после подтверждения на странице; повторно не создаём."""
    b = request.get_json(silent=True) or {}
    if not b.get("confirm"):
        return jsonify(ok=False, error="Нужно подтверждение"), 400
    nums = [str(n)[:20] for n in b.get("nums") or []][:30]
    res, st = {}, settings()
    if st["from"] == "office" and not st["office"]:
        return jsonify(ok=False, error="Укажите код пункта приёма СДЭК, куда сдаёте посылки (настройки вкладки)"), 400
    try:
        got = _pick(nums)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 503
    with db() as d:
        for n in nums:
            x, r = got.get(n), row(d, n)
            if r and r.get("uuid") and r.get("status") not in ("INVALID", "CANCELLED"):
                res[n] = "уже создано"
                continue
            if not x or not x["pvz"]:
                res[n] = "нет кода пункта выдачи в заказе" if x else "заказ не найден"
                continue
            if not x["phone"]:
                res[n] = "нет телефона покупателя"
                continue
            try:
                pt = point(x["pvz"])
                j = api("POST", "/orders", body(x, _goods(x["id"]), pt, st))
                uuid = ((j.get("entity") or {}).get("uuid")) or ""
                bad = [e.get("message") for q in j.get("requests") or [] for e in q.get("errors") or []]
                if not uuid or bad:
                    raise RuntimeError("; ".join(x for x in bad if x) or "СДЭК не вернул номер")
                save(d, n, order_id=x["id"], agent_id=x["agent"], uuid=uuid, status="CREATED", tariff=tariff_for(pt, st), err="")
                res[n] = "ok"
            except Exception as e:
                save(d, n, order_id=x["id"], agent_id=x["agent"], err=str(e)[:300])
                res[n] = str(e)[:200]
    ok = [k for k, v in res.items() if v == "ok"]
    if ok:                                               # номер СДЭК присваивает не сразу — подтянем через несколько секунд
        t = threading.Timer(4, _safe_refresh, args=(ok,))
        t.daemon = True
        t.start()
    return jsonify(ok=True, result=res)


def _safe_refresh(nums):
    try:
        refresh(set(nums))
    except Exception as e:
        print("СДЭК: обновление после создания", str(e)[:200], flush=True)


@bp.post("/admin/api/cdek/refresh")
@need("orders")
def route_refresh():
    try:
        refresh()
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 502
    return jsonify(ok=True)


@bp.route("/admin/api/cdek/print", methods=["GET", "POST"])
@need("orders")
def route_print():
    """PDF накладных (kind=orders) или наклеек (kind=barcodes) выбранных отправлений (?kind=&nums=1,2 или JSON)."""
    b = request.get_json(silent=True) or {}
    kind = "barcodes" if (b.get("kind") or request.args.get("kind")) == "barcodes" else "orders"
    nums = b.get("nums") or [x for x in (request.args.get("nums") or "").split(",") if x]
    with db() as d:
        uu = [r["uuid"] for r in (row(d, str(n)[:20]) for n in nums[:50]) if r and r.get("uuid")]
    if not uu:
        return jsonify(ok=False, error="Сначала создайте отправления"), 400
    try:
        req = {"orders": [{"order_uuid": u} for u in uu], "copy_count": 1}
        if kind == "barcodes":
            req["format"] = "A6"
        job = ((api("POST", f"/print/{kind}", req) or {}).get("entity") or {}).get("uuid")
        url = ""
        for _ in range(15):
            time.sleep(1)
            e = (api("GET", f"/print/{kind}/{job}") or {}).get("entity") or {}
            if e.get("url"):
                url = e["url"]
                break
        if not url:
            return jsonify(ok=False, error="СДЭК ещё готовит PDF — нажмите ещё раз через минуту"), 504
        pdf = api("GET", url.split("/v2", 1)[1], raw=True).content
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 502
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f"inline; filename=cdek-{kind}.pdf"})


@bp.post("/admin/api/cdek/intake")
@need("orders")
def route_intake():
    """Курьер СДЭК на забор со склада — на выбранный день (платно по договору; только по кнопке)."""
    b = request.get_json(silent=True) or {}
    day = str(b.get("date") or "")
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", day) or not b.get("confirm"):
        return jsonify(ok=False, error="Выберите день и подтвердите"), 400
    store = delivery.conf()["store"]
    phone = re.sub(r"\D", "", store.get("wh_phone") or "")
    with db() as d:
        rs = [r for r in (row(d, str(n)[:20]) for n in (b.get("nums") or [])[:50]) if r and r.get("uuid")]
    weight = 0
    for r in rs:
        try:
            weight += sum(q * w for _, _, q, _, w in _goods(r["order_id"]))
        except Exception:
            weight += 500
    try:
        j = api("POST", "/intakes", {
            "intake_date": day, "intake_time_from": str(b.get("from") or "10:00"), "intake_time_to": str(b.get("to") or "18:00"),
            "name": f"Косметика, {len(rs) or 1} отправл.", "weight": max(500, weight),
            "sender": {"company": "AMURA", "name": "AMURA", "phones": [{"number": "+" + phone}] if phone else []},
            "from_location": {"code": city_code("Алматы"), "address": store.get("wh_addr") or "Алматы"},
            "comment": "AMURA: отправления " + ", ".join(r["number"] or r["num"] for r in rs)[:200]})
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:300]), 502
    return jsonify(ok=True, uuid=((j.get("entity") or {}).get("uuid")) or "")


@bp.route("/admin/api/cdek/settings", methods=["PUT"])
@need("orders")
def route_settings():
    import admin
    if not admin.is_admin():
        return jsonify(ok=False, error="Настройки — только владелец"), 403
    try:
        return jsonify(ok=True, settings=save_settings(request.get_json(silent=True) or {}))
    except (TypeError, ValueError) as e:
        return jsonify(ok=False, error=str(e) or "Неверное значение"), 400
