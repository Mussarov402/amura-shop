"""Выключатели сайтов: выключенный сайт отдаёт в каталоге «closed» и не принимает заказы, другой сайт работает."""
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

from flask import Flask  # noqa: E402
import admin  # noqa: E402
import order_hook as oh  # noqa: E402


class Sites(unittest.TestCase):
    def setUp(self):
        self.patch(admin, "is_admin", lambda: True)
        self.patch(oh, "live", lambda: {"updated": "x", "items": []})
        self.patch(oh, "view_items", lambda items, ws: [])
        self.patch(oh, "session_cid", lambda: None)
        self.patch(oh, "order_core", lambda *a, **k: (dict(ok=True, number="1"), 200))
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        app.register_blueprint(oh.bp)
        self.c = app.test_client()
        oh._orders.clear()                    # повтор того же заказа отдаётся из кэша
        for k in oh.SITES:
            oh.set_site(k, True)
        self.addCleanup(lambda: [oh.set_site(k, True) for k in oh.SITES])

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def put(self, sid, on):
        return json.loads(self.c.put("/admin/api/sites", data=json.dumps({"id": sid, "on": on}), content_type="application/json").data)

    def order(self, mode=None):
        return self.c.post("/order", data=json.dumps({"name": "А", "mode": mode}), content_type="application/json")

    def test_retail_off(self):
        r = self.put("rtl", False)
        self.assertEqual({s["id"]: s["on"] for s in r["sites"]}, {"opt": True, "rtl": False})
        self.assertIn("closed", json.loads(self.c.get("/catalog?mode=retail").data))
        self.assertNotIn("closed", json.loads(self.c.get("/catalog").data))          # оптовый работает
        self.assertEqual(self.order("retail").status_code, 503)
        self.assertEqual(self.order().status_code, 200)
        self.put("rtl", True)
        self.assertNotIn("closed", json.loads(self.c.get("/catalog?mode=retail").data))
        self.assertEqual(self.order("retail").status_code, 200)

    def test_wholesale_off(self):
        self.put("opt", False)
        self.assertIn("closed", json.loads(self.c.get("/catalog").data))
        self.assertEqual(self.order().status_code, 503)
        self.assertEqual(self.order("retail").status_code, 200)


if __name__ == "__main__":
    unittest.main()
