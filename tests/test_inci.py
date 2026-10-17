"""Состав (INCI) в карточке товара: доп. поле «Состав» из МойСклад → выгрузка и /product/<id>; в живой каталог не входит."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402
os.environ.setdefault("MS_TOKEN", "test")
import export_catalog  # noqa: E402
import order_hook as oh  # noqa: E402

PID = "11111111-2222-3333-4444-555555555555"
ROW = {"id": PID, "name": "Крем", "code": "001", "description": "Увлажняет",
       "attributes": [{"name": "Бренд", "value": "Anua"}, {"name": "Состав", "value": " Aqua, Glycerin, Niacinamide "}],
       "salePrices": [], "updated": "2026-10-16 10:00:00"}


class Inci(unittest.TestCase):
    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def test_attr(self):
        self.assertEqual(oh._inci(ROW), "Aqua, Glycerin, Niacinamide")
        self.assertEqual(export_catalog.inci_of(ROW), "Aqua, Glycerin, Niacinamide")
        self.assertEqual(oh._inci({"attributes": [{"name": "Бренд", "value": "x"}]}), "")

    def test_live_and_product_route(self):
        self.patch(oh, "_stock", lambda: {PID: 5, "p2": 3})
        self.patch(oh, "_img_index", lambda: set())
        self.patch(oh, "cached", lambda k, ttl, fn: fn())
        import admin
        self.patch(admin, "hidden_ids", lambda: set())
        site_row = {"_site": True, "id": "p2", "name": "Тоник", "desc": "Тонизирует", "inci": "Aqua", "opt": 100, "rtl": 200}
        live = oh.build_live([site_row])
        self.assertNotIn("inci", live["items"][0])                    # клиентам каждые 2 минуты — без состава
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        c = app.test_client()
        self.assertEqual(json.loads(c.get("/product/p2").data), {"desc": "Тонизирует", "inci": "Aqua"})
        self.patch(oh, "ms", lambda method, path, **kw: dict(ROW))     # товара нет в кэше — берём из МойСклад
        self.assertEqual(json.loads(c.get("/product/" + PID).data)["inci"], "Aqua, Glycerin, Niacinamide")


if __name__ == "__main__":
    unittest.main()
