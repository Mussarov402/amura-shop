"""Корзина розницы как на WB: цена доставки по ступеням, СДЭК до двери +1000 всегда, пункты выдачи, интервалы Яндекс."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "fonts"), exist_ok=True)
for _f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
    if not os.path.exists(os.path.join(ROOT, "fonts", _f)):
        import shutil
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

import order_hook as oh  # noqa: E402
import delivery  # noqa: E402
import checkout  # noqa: E402

ITEM = {"id": "p1", "name": "Крем", "qty": 50, "opt": 5000, "mid": 4900, "box": 4800, "boxQty": 40, "rtl": 3000}


class Prices(unittest.TestCase):
    def test_tiers_and_door(self):
        cp = checkout.client_price
        self.assertEqual(cp(3000, "courier", "Алматы"), 1500)
        self.assertEqual(cp(10000, "courier", "Алматы"), 500)
        self.assertEqual(cp(17000, "cdek", "Караганда"), 500)
        self.assertEqual(cp(25000, "courier", "Алматы"), 0)
        self.assertEqual(cp(3000, "courier", "Караганда"), 2500)      # до двери +1000
        self.assertEqual(cp(25000, "courier", "Астана"), 1000)        # до двери платная и при бесплатной
        self.assertEqual(cp(25000, "cdek", "Астана"), 0)
        self.assertEqual(cp(3000, "pickup", "Астана"), 0)


class Options(unittest.TestCase):
    def setUp(self):
        checkout.cdek_eta = lambda city: {"pvz": (2, 4), "door": (3, 5)}
        checkout.cdek_points = lambda city: [{"code": "KRG1", "name": "ПВЗ", "address": "Бухар-Жырау 52", "type": "Пункт выдачи"}]
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(checkout.bp)
        self.c = app.test_client()

    def test_almaty(self):
        r = json.loads(self.c.get("/delivery/options?city=Алматы&sum=3000").data)
        self.assertTrue(r["almaty"])
        self.assertEqual([m["price"] for m in r["methods"]], [1500, 1500, 0])
        self.assertIsInstance(r["slots"], list)

    def test_region(self):
        r = json.loads(self.c.get("/delivery/options?city=Караганда&sum=3000").data)
        self.assertFalse(r["almaty"])
        self.assertEqual(r["slots"], [])
        self.assertEqual(r["methods"][0]["price"], 2500)
        self.assertIn("3–5 дн.", r["methods"][0]["note"])
        self.assertIn("2–4 дн.", r["methods"][1]["note"])
        p = json.loads(self.c.get("/delivery/pvz?city=Караганда").data)
        self.assertEqual(p["points"][0]["code"], "KRG1")


class Orders(unittest.TestCase):
    def setUp(self):
        self.posted = []
        oh.catalog = lambda: {"p1": dict(ITEM)}
        oh.too_many = lambda ip: False
        oh.find_or_create_agent = lambda *a: "agent-1"
        oh.organization = lambda: "org-1"
        oh.store_id = lambda: "store-1"
        oh.fee_service_id = lambda: "fee-1"
        oh.delivery_service_id = lambda: "dlv-1"
        oh.notify_bg = lambda fn: None
        oh._post_order = lambda body: self.posted.append(body) or {"name": "1600"}

    def order(self, **kw):
        d = {"name": "Аружан", "phone": "+77012345678", "city": "Алматы", "shipping": "courier", "mode": "retail",
             "items": [{"id": "p1", "qty": 1}], **kw}
        return oh.order_core(d, "o" + str(len(self.posted)) + json.dumps(kw, ensure_ascii=False), "1.1.1.1", None)

    def test_almaty_courier_slot(self):
        s = delivery.slots_ahead(days=7)
        self.assertTrue(s, "по графику должны быть интервалы")
        res, st = self.order(address="Абая 10, кв 5", slot=s[0])
        self.assertEqual(st, 200)
        self.assertEqual(res["total"], 3000 + 1500)
        body = self.posted[-1]
        self.assertIn("Курьер по Алматы (Яндекс) — Абая 10, кв 5", body["description"])
        self.assertEqual(body["positions"][-1]["price"], 150000)

    def test_bad_slot_and_no_address(self):
        self.assertEqual(self.order(address="Абая 10", slot={"date": "2000-01-01", "from": "08:00"})[1], 400)
        self.assertEqual(self.order()[1], 400)

    def test_region_door(self):
        res, st = self.order(city="Караганда", address="Ерубаева 1")
        self.assertEqual(st, 200)
        self.assertEqual(res["total"], 3000 + 2500)
        self.assertIn("Курьер СДЭК до двери — Ерубаева 1", self.posted[-1]["description"])

    def test_pvz_and_pickup(self):
        res, st = self.order(city="Астана", shipping="cdek", pvz={"code": "AST7", "address": "Кенесары 40"})
        self.assertEqual(st, 200)
        self.assertIn("Пункт выдачи СДЭК — Кенесары 40 (код AST7)", self.posted[-1]["description"])
        res, st = self.order(shipping="pickup")
        self.assertEqual(res["total"], 3000)
        self.assertEqual(len(self.posted[-1]["positions"]), 1)        # без позиции «Доставка»

    def test_browser_price_ignored(self):
        res, st = self.order(address="Абая 10", slot=delivery.slots_ahead(days=7)[0], delivery=0, total=1)
        self.assertEqual(res["total"], 4500)


if __name__ == "__main__":
    unittest.main()
