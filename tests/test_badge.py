"""Синяя галочка «оригинал» (25.11.2026): товар → бренд → по умолчанию вкл; правило бренда и для новых товаров;
переключатель товара переопределяет бренд в обе стороны; поле orig в живом каталоге; только владелец."""
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
import badge  # noqa: E402
import inbox  # noqa: E402
import order_hook as oh  # noqa: E402

LIVE = {"updated": "x", "items": [{"id": "a1", "code": "001", "name": "Anua тонер", "brand": "Anua", "qty": 5},
                                  {"id": "a2", "code": "002", "name": "Anua крем", "brand": "Anua", "qty": 2},
                                  {"id": "c1", "code": "003", "name": "COSRX эссенция", "brand": "COSRX", "qty": 1}],
        "hiddenItems": [], "outItems": [{"id": "a3", "code": "004", "name": "Anua новинка", "brand": "Anua", "qty": 0}]}


class Badge(unittest.TestCase):
    def setUp(self):
        with inbox.db() as d:
            inbox.set_setting(d, badge.KEY, "")
        self.addCleanup(lambda: badge._save({"brands": {}, "items": {}}))
        old_live, old_who = oh.live, admin.who
        oh.live = lambda max_age=None: json.loads(json.dumps(LIVE))
        self.role = "owner"
        admin.who = lambda: {"role": self.role, "perms": ["products"], "name": "x"}
        self.addCleanup(lambda: (setattr(oh, "live", old_live), setattr(admin, "who", old_who)))
        app = Flask(__name__)
        app.register_blueprint(badge.bp)
        self.c = app.test_client()

    def test_default_as_now(self):
        d = badge.apply(json.loads(json.dumps(LIVE)))
        self.assertTrue(all(i["orig"] for k in ("items", "outItems") for i in d[k]))   # по умолчанию — как было: у всех
        j = self.c.get("/admin/api/badge").get_json()
        self.assertEqual((j["on"], j["off"]), (4, 0))

    def test_brand_rule_and_item_override(self):
        self.c.put("/admin/api/badge", json={"brand": " anua ", "on": False, "doc": "есть документы поставки"})
        f = {i["id"]: i["orig"] for k in ("items", "outItems") for i in badge.apply(json.loads(json.dumps(LIVE)))[k]}
        self.assertEqual(f, {"a1": False, "a2": False, "c1": True, "a3": False})        # и новый товар бренда (a3)
        self.c.put("/admin/api/badge", json={"ids": ["a2"], "on": True})                 # товару включить при выключенном бренде
        self.c.put("/admin/api/badge", json={"ids": ["c1"], "on": False})                # и выключить при «по умолчанию»
        j = self.c.get("/admin/api/badge").get_json()
        r = {x["id"]: (x["on"], x["src"]) for x in j["items"]}
        self.assertEqual(r, {"a1": (False, "brand"), "a2": (True, "item"), "a3": (False, "brand"), "c1": (False, "item")})
        self.assertEqual(j["brands"][0]["doc"], "есть документы поставки")
        self.c.put("/admin/api/badge", json={"ids": ["a2"], "reset": True})              # «как у бренда»
        self.assertEqual(badge.flag({"id": "a2", "brand": "Anua"}), (False, "brand"))

    def test_owner_only_and_validation(self):
        self.assertEqual(self.c.put("/admin/api/badge", json={}).status_code, 400)
        self.role = "staff"
        self.assertEqual(self.c.get("/admin/api/badge").status_code, 403)
        self.assertEqual(self.c.put("/admin/api/badge", json={"brand": "Anua", "on": False}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
