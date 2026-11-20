"""Отправления СДЭК из системы AMURA (20.11.2026): заказы с пунктом выдачи СДЭК, расчёт без создания, создание только с подтверждением
(тариф по типу пункта, без наложенного платежа, ценность = товары), трек в описание МС, статусы (вручено → «Доставлен»),
трек покупателю, тестовый режим СДЭК — api.edu.cdek.ru."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask  # noqa: E402

import admin  # noqa: E402
import cdek_orders as co  # noqa: E402
import courier  # noqa: E402
import delivery  # noqa: E402
import order_hook as oh  # noqa: E402
import ship_label  # noqa: E402

OID = "11111111-2222-3333-4444-555555555555"
ORDER = {"id": OID, "name": "2190", "moment": "2026-11-20 10:00:00", "sum": 1450000, "payedSum": 1450000,
         "agent": {"id": "ag1", "name": "Дана"}, "state": {"name": "Собран"},
         "description": f"{oh.RETAIL_MARK}\nДана, WhatsApp +77017654321\nГород: Астана\n"
                        "Отправка: Пункт выдачи СДЭК — Астана, пр. Абая 10 (код AST12)"}
POS = {"rows": [{"quantity": 2, "price": 600000, "assortment": {"meta": {"type": "product"}, "code": "01300", "name": "Крем", "weight": 0.25}},
                {"quantity": 1, "price": 50000, "assortment": {"meta": {"type": "service"}, "name": "Доставка"}}]}


class Resp:
    def __init__(self, j, code=200, content=b""):
        self._j, self.status_code, self.text, self.content = j, code, json.dumps(j), content

    def json(self):
        return self._j

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Cdek(unittest.TestCase):
    def setUp(self):
        self.calls, self.puts, self.desc = [], [], ORDER["description"]
        self.status, self.test = "ACCEPTED", "0"

        def req(method, url, json=None, params=None, **kw):
            self.calls.append((method, url, json, params))
            path = url.split("/v2", 1)[1]
            if path == "/deliverypoints":
                return Resp([{"type": "PVZ", "location": {"city_code": 4756, "address_full": "Астана"}}])
            if path == "/location/cities":
                return Resp([{"code": 4961}])
            if path == "/calculator/tariff":
                return Resp({"total_sum": 1870})
            if path == "/orders" and method == "POST":
                return Resp({"entity": {"uuid": "uu-1"}, "requests": [{"state": "ACCEPTED", "errors": []}]})
            if path == "/orders/uu-1":
                return Resp({"entity": {"uuid": "uu-1", "cdek_number": "1234567890", "statuses": [{"code": self.status}],
                                        "delivery_detail": {"total_sum": 1900}}})
            if path == "/print/orders" and method == "POST":
                return Resp({"entity": {"uuid": "pr-1"}})
            if path == "/print/orders/pr-1":
                return Resp({"entity": {"url": "https://api.cdek.ru/v2/print/orders/pr-1.pdf"}})
            if path == "/print/orders/pr-1.pdf":
                return Resp({}, content=b"%PDF-1.4")
            raise AssertionError(path)

        def ms(method, path, params=None, json=None, **kw):
            if path == "/entity/customerorder":
                return {"rows": [{**ORDER, "description": self.desc}]}
            if path == f"/entity/customerorder/{OID}/positions":
                return POS
            if path == f"/entity/customerorder/{OID}" and method == "GET":
                return {**ORDER, "description": self.desc}
            if path == f"/entity/customerorder/{OID}" and method == "PUT":
                self.puts.append(json)
                if "description" in json:
                    self.desc = json["description"]
                return {}
            raise AssertionError(path)
        self.patch(co.requests, "request", req)
        self.patch(delivery.Cdek, "token", lambda s, timeout=20: "tok")
        self.patch(oh, "ms", ms)
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Т"})
        self.patch(courier, "set_ms_state", lambda oid, name: self.puts.append({"state": name}) or True)
        self.patch(delivery, "conf", lambda: {"svc": {"cdek": {"client_id": "id", "secret": "s", "test": self.test}, "yandex": {}},
                                              "store": {"wh_addr": "Байсат, склад", "wh_phone": "+7 701 000 00 00"},
                                              "rules": {"weight": 300, "box_w": 20, "box_h": 15, "box_d": 10}})
        for k in [k for k in oh._cache if str(k).startswith("cdek")]:
            oh._cache.pop(k)
        with co.db() as d:
            d.run("DELETE FROM cdek_ship")
        self.addCleanup(self.clean)
        app = Flask(__name__)
        app.register_blueprint(co.bp)
        self.c = app.test_client()

    @staticmethod
    def clean():
        with co.db() as d:
            d.run("DELETE FROM cdek_ship")

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def post(self, url, b):
        return json.loads(self.c.post(url, data=json.dumps(b), content_type="application/json").data)

    def test_list_and_parse(self):
        j = json.loads(self.c.get("/admin/api/cdek").data)
        x = j["orders"][0]
        self.assertEqual((x["num"], x["pvz"], x["city"], x["phone"], x["name"]), ("2190", "AST12", "Астана", "77017654321", "Дана"))
        self.assertEqual(x["statusText"], "Ещё не создано")

    def test_estimate_then_create_needs_confirm(self):
        r = self.post("/admin/api/cdek/estimate", {"nums": ["2190"]})
        self.assertEqual(r["result"]["2190"]["est"], 1870)
        self.assertFalse([c for c in self.calls if c[1].endswith("/orders") and c[0] == "POST"])      # расчёт ничего не создаёт
        self.assertEqual(self.c.post("/admin/api/cdek/create", json={"nums": ["2190"]}).status_code, 400)   # без подтверждения — нет
        r = self.post("/admin/api/cdek/create", {"nums": ["2190"], "confirm": True})
        self.assertEqual(r["result"]["2190"], "ok")
        b = [c for c in self.calls if c[1].endswith("/orders") and c[0] == "POST"][0][2]
        self.assertEqual((b["tariff_code"], b["delivery_point"], b["number"]), (138, "AST12", "AMURA-2190"))   # курьер со склада → ПВЗ
        self.assertEqual(b["delivery_recipient_cost"], {"value": 0})
        self.assertEqual(b["recipient"]["phones"], [{"number": "+77017654321"}])
        pk = b["packages"][0]
        self.assertEqual((pk["weight"], len(pk["items"]), pk["items"][0]["cost"], pk["items"][0]["payment"]), (500, 1, 6000, {"value": 0}))
        self.assertEqual(b["from_location"]["address"], "Байсат, склад")
        r = self.post("/admin/api/cdek/create", {"nums": ["2190"], "confirm": True})
        self.assertEqual(r["result"]["2190"], "уже создано")                                          # второй раз не создаём

    def test_refresh_track_state_and_client(self):
        self.post("/admin/api/cdek/create", {"nums": ["2190"], "confirm": True})
        co.refresh()
        self.assertIn("Трек СДЭК: 1234567890", self.desc)
        self.assertEqual(ship_label.parse({**ORDER, "description": self.desc})["track"], "1234567890")   # адресная этикетка видит трек
        t = courier.client_tracks("ag1")["2190"]
        self.assertEqual((t["kind"], t["link"]), ("cdek", co.TRACK_URL + "1234567890"))
        self.status = "DELIVERED"
        co.refresh()
        self.assertIn({"state": "Доставлен"}, self.puts)
        with co.db() as d:
            self.assertEqual((co.row(d, "2190")["status"], co.row(d, "2190")["price"]), ("DELIVERED", 1900))
        self.assertEqual(self.desc.count("Трек СДЭК"), 1)

    def test_print_and_test_mode(self):
        self.test = "1"
        self.post("/admin/api/cdek/create", {"nums": ["2190"], "confirm": True})
        self.assertTrue(all(c[1].startswith("https://api.edu.cdek.ru/v2") for c in self.calls))         # тестовый режим СДЭК
        self.patch(co.time, "sleep", lambda s: None)
        r = self.c.get("/admin/api/cdek/print?kind=orders&nums=2190")
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, "application/pdf", b"%PDF-1.4"))

    def test_office_needs_code(self):
        co.save_settings({"from": "office", "office": ""})
        self.addCleanup(lambda: co.save_settings({"from": "door"}))
        self.assertEqual(self.c.post("/admin/api/cdek/create", json={"nums": ["2190"], "confirm": True}).status_code, 400)
        co.save_settings({"from": "office", "office": "ALM5"})
        self.post("/admin/api/cdek/create", {"nums": ["2190"], "confirm": True})
        b = [c for c in self.calls if c[1].endswith("/orders") and c[0] == "POST"][0][2]
        self.assertEqual((b["tariff_code"], b["shipment_point"]), (136, "ALM5"))


if __name__ == "__main__":
    unittest.main()
