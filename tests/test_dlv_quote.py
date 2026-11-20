"""Настоящая цена доставки на рознице (dlv_quote.py): Яндекс check-price (окно «в течение дня», Express), СДЭК по весу,
запас +% и округление, бесплатно от порога (кроме Express), запасная цена при сбое / долгом ответе, кэш, заказ = показанная цена."""
import copy
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401  (общая настройка: SQLite, шрифты)
import app  # noqa: E402
import checkout  # noqa: E402
import courier  # noqa: E402
import delivery  # noqa: E402
import dlv_quote as dq  # noqa: E402
import order_hook as oh  # noqa: E402

CITY = "Алматы"
LINES = [{"id": "p1", "qty": 1}]


class Resp:
    def __init__(self, code, body):
        self.status_code, self.body, self.text = code, body, str(body)

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise dq.requests.HTTPError(str(self.status_code))


class Quote(unittest.TestCase):
    def setUp(self):
        for k in [k for k in oh._cache if isinstance(k, str) and k.startswith("dq:")]:
            oh._cache.pop(k)
        dq._down.update(ya=0.0, cdek=0.0)
        base = delivery.conf()
        self.rules = {}

        def conf():
            c = copy.deepcopy(base)
            c["svc"]["yandex"]["token"] = "ya-tok"
            c["store"]["wh_coords"] = "43.2389, 76.8897"
            c["rules"].update({"free_all": 50000, "sdd_price": 1000, "express_price": 2500, "cdek_price": 1500, "ya_margin": 10,
                               "cdek_margin": 0, "rpickup": 0, **self.rules})
            return c
        self.patch(delivery, "conf", conf)
        self.patch(delivery, "express_state", lambda now=None: {"open": True, "next": ""})
        self.patch(courier, "geocode", lambda a: (76.95, 43.24) if "туркебаева" in a.lower() else None)
        self.patch(checkout, "cdek_city_code", lambda c: {"алматы": 1, "астана": 2}.get(c.lower()))
        self.patch(checkout, "_cdek", lambda: (type("C", (), {"base": "https://cdek"})(), "cdek-tok"))
        self.patch(oh, "_weights", {"p1": 0.2, "p2": 1.0})
        oh._cache.pop("ya_methods_q", None)
        self.ivs = [{"from": "2026-11-21T05:00:00+00:00", "to": "2026-11-21T09:00:00+00:00"},     # 10–14 по Алматы
                    {"from": "2026-11-21T09:00:00+00:00", "to": "2026-11-21T13:00:00+00:00"}]     # 14–18
        self.claims, self.sdd_price, self.estimate_polls = [], 1650, 2
        dq._inflight.clear()

        def ya(path, body=None, **k):
            if path == "/delivery-methods":
                return {"same_day_delivery": {"allowed": True, "available_intervals": self.ivs}}
            if path == "/claims/create":
                self.claims.append({"id": f"c{len(self.claims) + 1}", "body": body, "polls": 0, "status": "estimating", "cancel": None})
                return {"id": self.claims[-1]["id"], "status": "estimating", "version": 1}
            c = next(x for x in self.claims if x["id"] == k["claim_id"])
            if path == "/claims/info":
                c["polls"] += 1
                if c["status"] == "estimating" and c["polls"] >= self.estimate_polls:
                    c["status"] = "ready_for_approval"
                out = {"id": c["id"], "status": c["status"], "version": 2, "available_cancel_state": "free"}
                if c["status"] == "ready_for_approval":
                    out["pricing"] = {"offer": {"price": str(self.sdd_price)}}
                return out
            if path == "/claims/cancel":
                c["cancel"], c["status"] = body, "cancelled"
                return {"status": "cancelled"}
            raise AssertionError(path)
        self.patch(courier, "ya", ya)
        self.patch(courier, "warehouse", lambda: {"addr": "Байсат", "phone": "77010000000", "pt": (76.8897, 43.2389)})
        self.patch(dq, "_nap", lambda s: None)
        self.sent, self.ya_price, self.fail_net, self.slow = [], 1650, None, 0

        def post(url, json=None, headers=None, timeout=None, **kw):
            self.sent.append((url, json))
            if self.slow:
                time.sleep(self.slow)
            if self.fail_net:
                raise dq.requests.ConnectionError("нет связи")
            if url.endswith("/check-price"):
                return Resp(200, {"price": str(self.ya_price), "currency_rules": {"code": "KZT"}})
            if url.endswith("/calculator/tarifflist"):
                kg = json["packages"][0]["weight"] / 1000
                return Resp(200, {"tariff_codes": [
                    {"delivery_mode": 4, "delivery_sum": 1200 + 400 * kg}, {"delivery_mode": 7, "delivery_sum": 1000 + 400 * kg},
                    {"delivery_mode": 3, "delivery_sum": 3000}]})
            raise AssertionError(url)
        self.patch(dq.requests, "post", post)

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(setattr, obj, name, old)

    def test_yandex_same_day_with_window_and_margin(self):
        slot = {"date": "2026-11-21", "from": "14:00", "to": "18:00", "name": "Вечер"}
        q = dq.quote("courier", CITY, 6690, LINES, "Туркебаева 63", slot)
        self.assertEqual(q, {"price": 1850, "real": True, "raw": 1650})       # 1650 × 1,1 = 1815 → вверх до 50 = 1850
        self.assertEqual(self.sent, [])                                        # «в течение дня» — не check-price, а оценка заявки
        c = self.claims[0]
        body = c["body"]
        self.assertEqual(body["route_points"][0]["address"]["coordinates"], [76.8897, 43.2389])   # склад
        self.assertEqual(body["route_points"][1]["address"]["coordinates"], [76.95, 43.24])
        self.assertEqual(body["same_day_data"]["delivery_interval"],                                # интервал — ровно как у Яндекса
                         {"from": "2026-11-21T09:00:00+00:00", "to": "2026-11-21T13:00:00+00:00"})
        self.assertNotIn("client_requirements", body)
        self.assertEqual(body["items"][0]["weight"], 0.2)
        self.assertEqual(c["cancel"], {"version": 2, "cancel_state": "free"})  # оценочную заявку сразу отменили
        dq.quote("courier", CITY, 6690, LINES, "Туркебаева 63", slot)          # тот же адрес и окно — из кэша, второй заявки нет
        self.assertEqual(len(self.claims), 1)
        dq.quote("courier", CITY, 6690, LINES, "Туркебаева 63", {**slot, "from": "10:00", "to": "14:00"})   # другое окно — новый расчёт
        self.assertEqual(len(self.claims), 2)
        self.assertEqual(dq.quote("courier", CITY, 6690, LINES, "Туркебаева 63")["real"], False)           # окно не выбрано — без заявки
        self.assertEqual(len(self.claims), 2)

    def test_sdd_slow_estimate_then_cache(self):
        self.patch(dq, "WAIT", 0.3)
        real_sleep = time.sleep
        self.patch(dq, "_nap", lambda s: real_sleep(0.2))
        self.estimate_polls = 4                                                # Яндекс считает дольше, чем ждёт покупатель
        slot = {"date": "2026-11-21", "from": "14:00", "to": "18:00"}
        q = dq.quote("courier", CITY, 6690, LINES, "Туркебаева 63", slot)
        self.assertEqual((q["price"], q["real"]), (1000, False))               # пока — запасная цена
        q2 = dq.quote("courier", CITY, 6690, LINES, "Туркебаева 63", slot)     # повторный запрос не создаёт вторую заявку
        self.assertEqual(len(self.claims), 1)
        real_sleep(1.2)
        q3 = dq.quote("courier", CITY, 6690, LINES, "Туркебаева 63", slot)     # оценка дошла в фоне — уже настоящая
        self.assertEqual((q3["price"], q3["real"]), (1850, True))
        self.assertEqual(len(self.claims), 1)
        self.assertEqual(q2["real"], False)

    def test_express_never_free_and_no_window(self):
        q = dq.quote("express", CITY, 90000, LINES, "Туркебаева 63")
        self.assertEqual(q["price"], 1850)
        self.assertEqual(self.sent[0][1]["requirements"], {"taxi_class": "courier"})

    def test_same_day_without_yandex_window_falls_back(self):
        self.ivs = [{"from": "2026-11-22T05:00:00+00:00", "to": "2026-11-22T09:00:00+00:00"}]   # на 21.11 окна у Яндекса нет
        q = dq.quote("courier", CITY, 6690, LINES, "Туркебаева 63", {"date": "2026-11-21", "from": "14:00", "to": "18:00"})
        self.assertEqual(q, {"price": 1000, "real": False, "raw": 0})          # не цена Express, а запасная «в течение дня»
        self.assertEqual((self.sent, self.claims), ([], []))

    def test_free_from_threshold(self):
        self.assertEqual(dq.quote("courier", CITY, 50000, LINES, "Туркебаева 63")["price"], 0)
        self.assertEqual(dq.quote("cdek", "Астана", 60000, LINES)["price"], 0)
        self.assertEqual(self.sent, [])                                        # бесплатно — службы не спрашиваем

    def test_cdek_weight_and_point_type(self):
        q = dq.quote("cdek", "Астана", 6690, [{"id": "p1", "qty": 2}, {"id": "nowt", "qty": 3}])
        body = self.sent[0][1]
        self.assertEqual(body["packages"][0]["weight"], 1900)                  # 2 × 0,2 + 3 × 0,5 (без веса в карточке)
        self.assertEqual(body["to_location"], {"code": 2})
        self.assertEqual(q["price"], 1960)                                     # пункт выдачи: 1200 + 400 × 1,9, запас 0 %
        p = dq.quote("cdek", "Астана", 6690, [{"id": "p1", "qty": 2}, {"id": "nowt", "qty": 3}], pvz={"type": "Постамат"})
        self.assertEqual(p["price"], 1760)                                     # постамат — свой тариф
        self.rules["cdek_margin"] = 5
        for k in [k for k in oh._cache if isinstance(k, str) and k.startswith("dq:")]:
            oh._cache.pop(k)
        self.assertEqual(dq.quote("cdek", "Астана", 6690, [{"id": "p1", "qty": 2}, {"id": "nowt", "qty": 3}])["price"], 2058)

    def test_fallback_on_error_and_timeout(self):
        self.fail_net = True
        q = dq.quote("express", CITY, 6690, LINES, "Туркебаева 63")
        self.assertEqual(q, {"price": 2500, "real": False, "raw": 0})          # запасная цена из настроек
        self.fail_net = False
        n = len(self.sent)
        self.assertEqual(dq.quote("express", CITY, 6690, LINES, "Туркебаева 63")["price"], 2500)   # пауза после сбоя — сразу запасная
        self.assertEqual(len(self.sent), n)
        dq._down["ya"] = 0.0
        self.patch(dq, "WAIT", 0.2)
        self.slow = 0.5
        t = time.time()
        q = dq.quote("express", CITY, 6690, LINES, "Туркебаева 63")
        self.assertLess(time.time() - t, 0.45)
        self.assertEqual(q["real"], False)
        self.assertEqual(q["price"], 2500)
        self.assertEqual(dq.quote("courier", CITY, 6690, LINES, "Неизвестная 1")["price"], 1000)   # адрес не найден — запасная

    def test_order_uses_quote_and_checks_shown(self):
        slot = {"date": "2026-11-21", "from": "14:00", "to": "18:00", "name": "Вечер"}
        self.patch(delivery, "slots_ahead", lambda now=None, days=7: [slot])
        d = {"address": "Туркебаева 63", "slot": slot, "dlvShown": 1850}
        price, text, err = checkout.order_delivery(d, "courier", CITY, 6690, lines=LINES)
        self.assertEqual((price, err), (1850, None))
        self.assertIn("Туркебаева 63", text)
        price, _, err = checkout.order_delivery({**d, "dlvShown": 1000}, "courier", CITY, 6690, lines=LINES)
        self.assertEqual(price, 1850)
        self.assertIn("Цена доставки обновилась: 1", err)
        self.assertEqual(checkout.order_delivery({"pvz": {"code": "AST1", "address": "Мангилик Ел 1"}}, "cdek", "Астана", 6690, lines=LINES)[0], 1280)

    def test_route(self):
        self.patch(oh, "too_many", lambda *a, **k: False)
        c = app.app.test_client()
        slot = {"date": "2026-11-21", "from": "14:00", "to": "18:00"}
        r = c.post("/delivery/quote", json={"ship": "courier", "city": CITY, "address": "Туркебаева 63", "slot": slot, "sum": 6690,
                                            "items": [{"id": "p1", "qty": 1}]})
        self.assertEqual(r.get_json(), {"ok": True, "ship": "courier", "price": 1850, "real": True})
        self.assertEqual(c.post("/delivery/quote", json={"ship": "courier", "city": "Астана", "address": "x"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
