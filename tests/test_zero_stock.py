"""Товар без остатка: в системе AMURA находится и добавляется в заказ (резерв — не больше остатка), сайт по-прежнему не продаёт его."""
import json
import os
import sys
import tempfile
import time
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

from flask import Flask  # noqa: E402
import admin  # noqa: E402
import order_hook as oh  # noqa: E402

IN = {"id": "a" * 8 + "-aaaa-aaaa-aaaa-" + "a" * 12, "name": "Крем", "brand": "Anua", "code": "001", "qty": 3, "opt": 5000, "mid": 0, "box": 0, "boxQty": 0, "rtl": 6690}
OUT = {"id": "b" * 8 + "-bbbb-bbbb-bbbb-" + "b" * 12, "name": "Тонер Anua", "brand": "Anua", "code": "002", "qty": 0, "opt": 4000, "mid": 0, "box": 0, "boxQty": 0, "rtl": 5000}


class ZeroStock(unittest.TestCase):
    def setUp(self):
        self.posted = []
        data = {"updated": "x", "items": [dict(IN)], "hiddenItems": [], "outItems": [dict(OUT)]}
        old = oh._cache.get("live")
        oh._cache["live"] = (time.time() + 1e6, data)
        self.addCleanup(lambda: oh._cache.__setitem__("live", old) if old else oh._cache.pop("live", None))
        for name, val in {"too_many": lambda *a, **k: False, "find_or_create_agent": lambda *a: "agent-1", "organization": lambda: "org-1",
                          "store_id": lambda: "store-1", "fee_service_id": lambda: "fee-1", "notify_bg": lambda fn: None, "is_wholesale": lambda cid: True,
                          "opt_min": lambda: 0,
                          "live": lambda max_age=None: data, "catalog": lambda: {i["id"]: i for i in data["items"]},
                          "_post_order": lambda body: self.posted.append(body) or {"name": "1700", "id": "o1"}}.items():
            if hasattr(oh, name):
                self.patch(oh, name, val)

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def order(self, panel, items):
        d = {"name": "К", "phone": "+77010000000", "city": "Алматы", "shipping": "pickup", "items": items}
        return oh.order_core(d, "k%d%s" % (len(self.posted), panel), "1.1.1.1", None, panel=panel)

    def test_panel_adds_out_of_stock_without_reserve(self):
        res, st = self.order(True, [{"id": IN["id"], "qty": 5}, {"id": OUT["id"], "qty": 2}])
        self.assertEqual(st, 200)
        pos = {p["assortment"]["meta"]["href"].rsplit("/", 1)[1]: p for p in self.posted[-1]["positions"] if "product/" in p["assortment"]["meta"]["href"]}
        self.assertEqual((pos[IN["id"]]["quantity"], pos[IN["id"]]["reserve"]), (5, 3))      # больше остатка — резерв на остаток
        self.assertEqual((pos[OUT["id"]]["quantity"], pos[OUT["id"]]["reserve"]), (2, 0))    # нет в наличии — без резерва

    def test_site_still_skips_out_of_stock(self):
        res, st = self.order(False, [{"id": IN["id"], "qty": 5}, {"id": OUT["id"], "qty": 2}])
        self.assertEqual(st, 200)
        prods = [p for p in self.posted[-1]["positions"] if "product/" in p["assortment"]["meta"]["href"]]
        self.assertEqual([(p["quantity"], p["reserve"]) for p in prods], [(3, 3)])           # сайт: только наличие, как раньше
        self.assertNotIn(OUT["id"], json.dumps(oh.view_items(oh._cache["live"][1]["items"], True)))

    def test_search_finds_out_of_stock(self):
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "В"})
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        r = json.loads(app.test_client().get("/admin/api/orders-search?q=anua").data)
        self.assertEqual({i["name"]: i["qty"] for i in r["items"]}, {"Крем": 3, "Тонер Anua": 0})


if __name__ == "__main__":
    unittest.main()
