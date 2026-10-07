"""Розничный сайт: заказ по розничным ценам с пометкой, каталог ?mode=retail, shop/index.html совпадает с index.html."""
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
import make_shop  # noqa: E402

ITEM = {"id": "p1", "name": "Крем", "qty": 50, "opt": 5000, "mid": 4900, "box": 4800, "boxQty": 40, "rtl": 6690}


class RetailOrder(unittest.TestCase):
    def setUp(self):
        self.posted = []
        oh.catalog = lambda: {"p1": dict(ITEM)}
        oh.too_many = lambda ip: False
        oh.find_or_create_agent = lambda *a: "agent-1"
        oh.organization = lambda: "org-1"
        oh.store_id = lambda: "store-1"
        oh.fee_service_id = lambda: "fee-1"
        oh.notify_bg = lambda fn: None
        oh._post_order = lambda body: self.posted.append(body) or {"name": "1530"}

    def order(self, mode=None, qty=12):
        d = {"name": "Тамирис", "phone": "+77012345678", "city": "Алматы", "shipping": "pickup", "items": [{"id": "p1", "qty": qty}]}
        if mode:
            d["mode"] = mode
        return oh.order_core(d, "k" + str(len(self.posted)), "1.1.1.1", None)

    def test_retail_prices_and_mark(self):
        res, st = self.order("retail")
        self.assertEqual(st, 200)
        body = self.posted[-1]
        self.assertEqual(body["positions"][0]["price"], 669000)             # розничная цена, без ступени «от 10 шт»
        self.assertTrue(body["description"].startswith(oh.RETAIL_MARK))

    def test_wholesale_unchanged(self):
        res, st = self.order()
        self.assertEqual(st, 200)
        body = self.posted[-1]
        self.assertEqual(body["positions"][0]["price"], 490000)             # опт: 12 шт → цена «от 10 шт»
        self.assertTrue(body["description"].startswith("Заказ с сайта"))

    def test_panel_order_ignores_mode(self):
        d = {"name": "М", "phone": "+77012345678", "city": "Алматы", "shipping": "pickup", "items": [{"id": "p1", "qty": 1}], "mode": "retail"}
        res, st = oh.order_core(d, "kp", "1.1.1.1", None, source="из панели", panel=True)
        self.assertEqual(st, 200)
        self.assertEqual(self.posted[-1]["positions"][0]["price"], 500000)


class RetailCatalog(unittest.TestCase):
    def test_mode_retail(self):
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        oh.live = lambda: {"updated": "x", "items": [dict(ITEM)]}
        oh.PUBLIC_WHOLESALE = True
        c = app.test_client()
        r = json.loads(c.get("/catalog?mode=retail").data)
        self.assertFalse(r["wholesale"])
        self.assertEqual(r["items"][0]["opt"], 6690)
        self.assertEqual(r["items"][0]["mid"], 0)
        w = json.loads(c.get("/catalog").data)
        self.assertTrue(w["wholesale"])
        self.assertEqual(w["items"][0]["opt"], 5000)


class ShopPage(unittest.TestCase):
    def test_shop_in_sync(self):
        with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
            want = make_shop.build(f.read())
        with open(os.path.join(ROOT, "shop", "index.html"), encoding="utf-8") as f:
            self.assertEqual(f.read(), want, "shop/index.html устарел — запустите python make_shop.py")
        self.assertIn("window.AMURA_RETAIL = true", want)


if __name__ == "__main__":
    unittest.main()
