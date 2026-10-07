"""Модуль «Доставка»: подключение служб доставки и правила цены для клиента.

Шаг 1 (этот файл): настройки в панели (Обзор → Доставка) — ключи Яндекс Доставки и СДЭК с проверкой подключения,
склад, фиксированная цена и порог бесплатной доставки по Алматы и по Казахстану, вес посылки по умолчанию.
Следующие шаги: цена в корзине сайта, автоматический вызов курьера, статусы и отслеживание.
Службы подключаемые (PROVIDERS); выключенная служба ни на что не влияет. Ключи хранятся на сервере и в панель целиком не отдаются.
"""
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

    def token(self):
        if not (self.cid and self.secret):
            raise RuntimeError("Не указаны Client ID и Client Secret")
        r = requests.post(self.base + "/oauth/token", data={"grant_type": "client_credentials", "client_id": self.cid,
                                                            "client_secret": self.secret}, timeout=TIMEOUT)
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
    "city_price": 990, "city_free": 5000,        # по Алматы: до порога — фиксированная цена, от порога — бесплатно
    "kz_price": 1990, "kz_free": 15000,          # по Казахстану
    "weight": 300,                               # вес посылки на 1 товар, г (в МойСклад у товаров вес 0)
}
STORE = ("wh_addr", "wh_phone", "wh_hours")


def _k(svc, f):
    return f"dlv_{svc}_{f}"


def conf():
    with inbox.db() as d:
        g = lambda k, v="": inbox.get_setting(d, k, v)  # noqa: E731
        svcs = {s: {"on": g(_k(s, "on"), "0") == "1", "test": g(_k(s, "test"), "0"),
                    **{f: g(_k(s, f)) for f, _, _ in P.fields}} for s, P in PROVIDERS.items()}
        rules = {k: int(g("dlv_" + k, str(v)) or v) for k, v in RULES.items()}
        store = {k: g("dlv_" + k) for k in STORE}
    return {"svc": svcs, "rules": rules, "store": store}


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
    return {"services": out, "rules": c["rules"], "store": c["store"]}


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
    oh._cache.pop("dlv_conf", None)


def test(svc):
    if svc not in PROVIDERS:
        raise ValueError("Неизвестная служба")
    return PROVIDERS[svc](conf()["svc"][svc]).test()


def price(goods_sum, zone):
    """Цена доставки для клиента: zone = "city" (Алматы) или "kz". До порога — фиксированная, от порога — 0."""
    r = conf()["rules"]
    return 0 if goods_sum >= r[zone + "_free"] else r[zone + "_price"]
