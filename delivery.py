"""Модуль «Доставка»: подключение служб доставки и правила цены для клиента.

Шаг 1 (этот файл): настройки в панели (Обзор → Доставка) — ключи Яндекс Доставки и СДЭК с проверкой подключения,
склад, цена для клиента как на Kaspi (единая в пункт выдачи / постамат и единая до двери, от порога — бесплатно),
график работы и интервалы доставки, из которых клиент выбирает удобный, вес посылки по умолчанию.
Следующие шаги: цена в корзине сайта, автоматический вызов курьера, статусы и отслеживание.
Службы подключаемые (PROVIDERS); выключенная служба ни на что не влияет. Ключи хранятся на сервере и в панель целиком не отдаются.
"""
import json
import re
from datetime import datetime, timedelta

import requests

import inbox
import order_hook as oh

TIMEOUT = 20


class Yandex:
    name = "Яндекс Доставка"
    BASE = "https://b2b.taxi.yandex.net"
    fields = (("token", "Ключ API (OAuth-токен)", True),)

    def __init__(self, c):
        self.token = c.get("token", "")

    def test(self):
        """Ключ проверяем запросом списка заявок: ничего не создаёт и ничего не стоит."""
        if not self.token:
            raise RuntimeError("Не указан ключ API")
        r = requests.post(self.BASE + "/b2b/cargo/integration/v2/claims/search",
                          headers={"Authorization": "Bearer " + self.token, "Accept-Language": "ru"},
                          json={"offset": 0, "limit": 1}, timeout=TIMEOUT)
        if r.status_code in (401, 403):
            raise RuntimeError("Яндекс не принял ключ — проверьте, что скопирован полностью")
        if r.status_code >= 400:
            raise RuntimeError(f"Яндекс ответил {r.status_code}: {r.text[:200]}")
        return "ключ принят"


class Cdek:
    name = "СДЭК"
    fields = (("client_id", "Аккаунт (Client ID)", False), ("secret", "Секретный ключ (Client Secret)", True))

    def __init__(self, c):
        self.cid, self.secret, self.test_mode = c.get("client_id", ""), c.get("secret", ""), c.get("test") == "1"
        self.base = "https://api.edu.cdek.ru/v2" if self.test_mode else "https://api.cdek.ru/v2"

    def token(self, timeout=TIMEOUT):
        if not (self.cid and self.secret):
            raise RuntimeError("Не указаны Client ID и Client Secret")
        r = requests.post(self.base + "/oauth/token", data={"grant_type": "client_credentials", "client_id": self.cid,
                                                            "client_secret": self.secret}, timeout=timeout)
        if r.status_code in (400, 401, 403):
            raise RuntimeError("СДЭК не принял Client ID / Client Secret")
        r.raise_for_status()
        return r.json()["access_token"]

    def test(self):
        tok = self.token()
        r = requests.get(self.base + "/location/cities", params={"country_codes": "KZ", "city": "Алматы", "size": 1},
                         headers={"Authorization": "Bearer " + tok}, timeout=TIMEOUT)
        r.raise_for_status()
        city = (r.json() or [{}])[0]
        return "ключи приняты" + (f", город отправки найден: {city.get('city')} (код {city.get('code')})" if city.get("code") else "")


PROVIDERS = {"yandex": Yandex, "cdek": Cdek}
SECRET = {"token", "secret"}

# правила цены для клиента и склад: ключ настройки → (подпись, значение по умолчанию)
RULES = {
    "free_from": 20000,          # от этой суммы товаров доставка бесплатная; 0 — бесплатной нет
    "door_extra": 1000,          # СДЭК до двери — доплата к цене (всегда, даже при бесплатной доставке)
    "weight": 300,               # вес на 1 товар, г — если в карточке МойСклад поле «Вес» пустое
    "box_w": 20, "box_h": 15, "box_d": 10,   # коробка по умолчанию, см — если у товаров не заполнены ШВГ
    "cutoff": 90,                # интервал можно выбрать, если до его начала больше N минут
    # розница: своя цена и порог бесплатной доставки у каждого способа (0 в пороге — бесплатной нет)
    "sdd_price": 1000, "sdd_free": 15000,        # Алматы, Яндекс «в течение дня»
    "express_price": 2500,                       # Алматы, срочный курьер Яндекс Экспресс — всегда платно
    "cdek_price": 1500, "cdek_free": 20000,      # другие города, пункт выдачи СДЭК
    "xauto": 1, "xauto_limit": 3000,             # Express: «Собран» → курьер Яндекс («Курьер») вызывается сам, если цена не выше лимита
}
# график: дни недели Пн…Вс (1 — работаем), часы работы и интервалы доставки курьером
SCHEDULE_DEFAULT = {"days": "0111111", "open": "10:00", "close": "18:00",       # окна как у Яндекса «в течение дня» (за 4 часа)
                    "slots": [{"name": "День", "from": "10:00", "to": "14:00"},
                              {"name": "Вечер", "from": "14:00", "to": "18:00"}]}
DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
# Express на сайте: часы приёма заказов по дням Пн…Вс (None — в этот день Express нет)
EXPRESS_HOURS_DEFAULT = [None, ["09:30", "17:30"], ["09:30", "17:30"], ["09:30", "17:30"], ["09:30", "17:30"], ["09:30", "17:30"], ["09:30", "15:00"]]
# цена доставки по сумме товаров — одна для всех способов (курьер, СДЭК), самовывоз бесплатно:
# [[до суммы, цена], …] — первая строка, где сумма меньше «до суммы»
TIERS_DEFAULT = [[5000, 1500], [15000, 500], [20000, 500]]
STORE = ("wh_addr", "wh_phone", "wh_hours", "wh_coords")   # wh_coords — точка склада для Яндекса («43.23, 76.94» или ссылка с карты)


def parse_coords(s):
    """«43.23, 76.94» или ссылка с карты (2ГИС, Яндекс Карты, Google) → (lon, lat) или None.
    В Казахстане долгота (46–87) всегда больше широты (40–56) — порядок чисел в ссылке не важен."""
    s = str(s or "").replace("%2C", ",").replace("%2c", ",")
    m = re.search(r"(-?\d{2}\.\d{2,})\s*[,; ]\s*(-?\d{2}\.\d{2,})", s)
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    lon, lat = max(a, b), min(a, b)
    return (lon, lat) if 40 <= lat <= 56 and 46 <= lon <= 88 else None


def resolve_coords(s):
    """Как parse_coords, но короткие ссылки «Поделиться» (go.2gis.com, yandex.kz/maps/-/…, maps.app.goo.gl) сначала открываем."""
    pt = parse_coords(s)
    url = re.search(r"https?://\S+", str(s or ""))
    if pt or not url:
        return pt
    def follow():
        r = requests.get(url.group(0), timeout=10, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (iPhone) AMURA"})
        p = parse_coords(r.url) or parse_coords(r.text[:300000])
        return list(p) if p else []
    try:
        res = oh.cached("coords:" + url.group(0), 86400 * 30, follow)
    except Exception as e:
        print("Доставка: координаты по ссылке", str(e)[:200], flush=True)
        return None
    return tuple(res) if res else None


def _k(svc, f):
    return f"dlv_{svc}_{f}"


def conf():
    with inbox.db() as d:
        g = lambda k, v="": inbox.get_setting(d, k, v)  # noqa: E731
        svcs = {s: {"on": g(_k(s, "on"), "0") == "1", "test": g(_k(s, "test"), "0"),
                    **{f: g(_k(s, f)) for f, _, _ in P.fields}} for s, P in PROVIDERS.items()}
        rules = {k: int(g("dlv_" + k, str(v)) or v) for k, v in RULES.items()}
        store = {k: g("dlv_" + k) for k in STORE}
        try:
            sched = {**SCHEDULE_DEFAULT, **json.loads(g("dlv_schedule") or "{}")}
        except ValueError:
            sched = dict(SCHEDULE_DEFAULT)
        try:
            tiers = json.loads(g("dlv_tiers") or "null") or TIERS_DEFAULT
        except ValueError:
            tiers = TIERS_DEFAULT
        try:
            xh = _clean_express_hours(json.loads(g("dlv_express_hours") or "null") or EXPRESS_HOURS_DEFAULT)
        except ValueError:
            xh = EXPRESS_HOURS_DEFAULT
    return {"svc": svcs, "rules": rules, "store": store, "schedule": sched, "tiers": tiers, "express_hours": xh}


def public_conf():
    """Для панели: секреты не отдаём — только признак «сохранён» и последние 4 символа."""
    c = conf()
    out = {}
    for s, P in PROVIDERS.items():
        v = c["svc"][s]
        fields = []
        for f, label, secret in P.fields:
            val = v.get(f, "")
            fields.append({"key": f, "label": label, "secret": secret, "set": bool(val),
                           "value": "" if secret else val, "tail": val[-4:] if secret and val else ""})
        out[s] = {"name": P.name, "on": v["on"], "test": v["test"] == "1", "fields": fields,
                  "ready": all(v.get(f) for f, _, _ in P.fields)}
    pt = resolve_coords(c["store"].get("wh_coords")) if c["store"].get("wh_coords") else None
    return {"services": out, "rules": c["rules"], "store": {**c["store"], "wh_point": [pt[1], pt[0]] if pt else None},
            "schedule": c["schedule"], "tiers": c["tiers"], "days": DAYS, "express_hours": c["express_hours"]}


def save(b):
    with inbox.db() as d:
        for s, P in PROVIDERS.items():
            v = b.get(s) or {}
            if "on" in v:
                inbox.set_setting(d, _k(s, "on"), "1" if v["on"] else "0")
            if "test" in v:
                inbox.set_setting(d, _k(s, "test"), "1" if v["test"] else "0")
            for f, _, secret in P.fields:
                if f in v:
                    val = str(v[f] or "").strip()[:500]
                    if val or not secret:              # пустое поле секрета — оставить сохранённое
                        inbox.set_setting(d, _k(s, f), val)
        for k, dflt in RULES.items():
            if k in (b.get("rules") or {}):
                try:
                    inbox.set_setting(d, "dlv_" + k, str(max(0, int(round(float(b["rules"][k]))))))
                except (TypeError, ValueError):
                    pass
        for k in STORE:
            if k in (b.get("store") or {}):
                inbox.set_setting(d, "dlv_" + k, str(b["store"][k] or "").strip()[:300])
        if isinstance(b.get("tiers"), list):
            inbox.set_setting(d, "dlv_tiers", json.dumps(_clean_tiers(b["tiers"])))
        if isinstance(b.get("express_hours"), list):
            inbox.set_setting(d, "dlv_express_hours", json.dumps(_clean_express_hours(b["express_hours"])))
        if isinstance(b.get("schedule"), dict):
            inbox.set_setting(d, "dlv_schedule", json.dumps(_clean_schedule(b["schedule"]), ensure_ascii=False))
    oh._cache.pop("dlv_conf", None)


def test(svc):
    if svc not in PROVIDERS:
        raise ValueError("Неизвестная служба")
    return PROVIDERS[svc](conf()["svc"][svc]).test()


def _hm(v, dflt):
    try:
        h, m = (int(x) for x in str(v).split(":"))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return f"{h:02d}:{m:02d}"
    except (TypeError, ValueError):
        pass
    return dflt


def _clean_schedule(sc):
    days = "".join("1" if c == "1" else "0" for c in str(sc.get("days", SCHEDULE_DEFAULT["days"]))[:7]).ljust(7, "0")
    slots = []
    for x in (sc.get("slots") or [])[:6]:
        name = str(x.get("name", "")).strip()[:30]
        a, b = _hm(x.get("from"), ""), _hm(x.get("to"), "")
        if name and a and b and a < b:
            slots.append({"name": name, "from": a, "to": b})
    slots.sort(key=lambda x: x["from"])
    return {"days": days, "open": _hm(sc.get("open"), "08:00"), "close": _hm(sc.get("close"), "18:00"), "slots": slots}


def _clean_express_hours(rows):
    out = []
    for i in range(7):
        x = rows[i] if isinstance(rows, list) and i < len(rows) else None
        a, b = (_hm(x[0], ""), _hm(x[1], "")) if isinstance(x, (list, tuple)) and len(x) == 2 else ("", "")
        out.append([a, b] if a and b and a < b else None)
    return out


def express_state(now=None):
    """Принимаем ли Express сейчас (время Алматы) и когда откроется. → {"open": bool, "next": "вт 09:30", "today": "до 17:30"}"""
    from datetime import datetime, timedelta
    now = now or datetime.now(oh.ALMATY)
    hours = conf()["express_hours"]
    hm = now.strftime("%H:%M")
    today = hours[now.weekday()]
    if today and today[0] <= hm < today[1]:
        return {"open": True, "next": "", "today": "до " + today[1]}
    for k in range(0, 8):
        d = now + timedelta(days=k)
        h = hours[d.weekday()]
        if h and (k > 0 or hm < h[0]):
            when = "сегодня" if k == 0 else "завтра" if k == 1 else DAYS[d.weekday()].lower()
            return {"open": False, "next": f"{when} в {h[0]}", "today": ""}
    return {"open": False, "next": "", "today": ""}


def express_hours_text():
    """«Вт–Сб 09:30–17:30, Вс 09:30–15:00» — для подсказки покупателю."""
    hours, parts, i = conf()["express_hours"], [], 0
    while i < 7:
        j = i
        while j + 1 < 7 and hours[j + 1] == hours[i]:
            j += 1
        if hours[i]:
            parts.append((DAYS[i] if i == j else f"{DAYS[i]}–{DAYS[j]}") + f" {hours[i][0]}–{hours[i][1]}")
        i = j + 1
    return ", ".join(parts)


def _clean_tiers(rows):
    out = {}
    for x in rows[:10]:
        try:
            upto, pr = int(round(float(x[0]))), int(round(float(x[1])))
        except (TypeError, ValueError, IndexError):
            continue
        if upto > 0 and pr >= 0:
            out[upto] = pr
    return [[k, out[k]] for k in sorted(out)]


def price(goods_sum, kind="courier"):
    """Цена доставки для клиента по сумме товаров — одна для курьера и СДЭК; самовывоз — 0.
    От порога free_from — бесплатно (0 — бесплатной нет); иначе первая ступень, где сумма меньше «до суммы»."""
    if kind == "pickup":
        return 0
    c = conf()
    ff = c["rules"]["free_from"]
    if ff and goods_sum >= ff:
        return 0
    tiers = c["tiers"]
    for upto, pr in tiers:
        if goods_sum < upto:
            return pr
    return tiers[-1][1] if tiers else 0


def slots_ahead(now=None, days=7):
    """Интервалы доставки курьером на ближайшие дни по графику (время Алматы): [{date, day, name, from, to}].
    Интервал доступен, если до его начала больше cutoff минут; выходные дни пропускаются."""
    c = conf()
    sc, cutoff = c["schedule"], c["rules"]["cutoff"]
    now = now or datetime.now(oh.ALMATY).replace(tzinfo=None)
    out = []
    for i in range(days + 7):
        day = (now + timedelta(days=i)).date()
        if sc["days"][day.weekday()] != "1":
            continue
        for s in sc["slots"]:
            start = datetime.combine(day, datetime.strptime(s["from"], "%H:%M").time())
            if start - now > timedelta(minutes=cutoff):
                out.append({"date": day.isoformat(), "day": DAYS[day.weekday()], **s})
        if len({x["date"] for x in out}) >= days:
            break
    return out


# ---------- габариты товара в МойСклад ----------
# «Вес» и «Объём» — стандартные поля карточки товара; ширины/высоты/глубины в МойСклад нет — добавляем доп. полями
DIM_ATTRS = ("Ширина, см", "Высота, см", "Глубина, см")


def ensure_dims():
    """Создаёт в карточке товара МойСклад доп. поля «Ширина, см», «Высота, см», «Глубина, см», если их ещё нет."""
    import admin
    for n in DIM_ATTRS:
        admin._attr("product", n, "double")


def _boot():
    import time
    time.sleep(45)
    try:
        ensure_dims()
        print("Доставка: поля ШВГ в карточке товара МойСклад на месте", flush=True)
    except Exception as e:
        print("Доставка: поля ШВГ не созданы:", str(e)[:200], flush=True)
    import delivery_estimate
    delivery_estimate.boot()


if __import__("os").environ.get("PORT"):
    import threading
    threading.Thread(target=_boot, daemon=True).start()
