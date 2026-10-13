"""Минимальный заказ на оптовом сайте: для новых покупателей; постоянные (есть заказы или метка «без минимума») — без минимума.
Розница, панель и ИИ — без ограничения."""
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

ITEM = {"id": "p1", "name": "Крем", "qty": 50, "opt": 5000, "mid": 4900, "box": 4800, "boxQty": 40, "rtl": 6690}


class OptMin(unittest.TestCase):
    def setUp(self):
        self.posted, self.orders, self.tags = [], {}, {}
        for k in [k for k in list(oh._cache) if isinstance(k, str) and k.startswith(("minfree:", "optmin"))]:
            oh._cache.pop(k)

        def ms(method, path, **kw):
            if path.startswith("/entity/counterparty/"):
                return {"tags": self.tags.get(path.rsplit("/", 1)[1], [])}
            if path == "/entity/customerorder":
                cid = kw["params"]["filter"].rsplit("/", 1)[1]
                return {"meta": {"size": self.orders.get(cid, 0)}, "rows": []}
            raise AssertionError(path)
        for name, val in {"ms": ms, "catalog": lambda: {"p1": dict(ITEM)}, "too_many": lambda *a, **k: False,
                          "find_or_create_agent": lambda *a: "agent-1", "organization": lambda: "org-1", "store_id": lambda: "store-1",
                          "fee_service_id": lambda: "fee-1", "delivery_service_id": lambda: "dlv-1", "notify_bg": lambda fn: None,
                          "is_wholesale": lambda cid: True, "cp_by_phone": lambda phone: {"id": "cp-" + phone[-4:]} if phone.endswith("1111") else None,
                          "_post_order": lambda body: self.posted.append(body) or {"name": "1600", "id": "o1"}}.items():
            self.patch(oh, name, val)
        oh.set_opt_min(20000)
        self.addCleanup(lambda: oh.set_opt_min(oh.OPT_MIN_DEFAULT))

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def order(self, qty=2, phone="+77010000000", me=None, **kw):
        d = {"name": "К", "phone": phone, "city": "Алматы", "shipping": "pickup", "items": [{"id": "p1", "qty": qty}]} | kw
        return oh.order_core(d, "k%d%s%s" % (len(self.posted), phone, me), "1.1.1.1", me, **({"source": kw.pop("source")} if "source" in kw else {}))

    def test_new_customer_below_minimum(self):
        res, st = self.order(qty=2)                                  # 10 000 ₸ < 20 000
        self.assertEqual(st, 400)
        self.assertIn("20 000", res["error"])
        self.assertEqual(self.order(qty=4)[1], 200)                  # 20 000 — можно

    def test_regular_by_phone_or_login_or_tag(self):
        self.orders["cp-1111"] = 3                                   # Камила: уже заказывала, вошла не через сайт — узнаём по телефону
        self.assertEqual(self.order(qty=1, phone="+77011111111")[1], 200)
        self.orders["cid-2"] = 1
        self.assertEqual(self.order(qty=1, me="cid-2")[1], 200)      # вошла на сайт
        self.tags["cid-3"] = ["Без минимума"]
        self.assertEqual(self.order(qty=1, me="cid-3")[1], 200)      # метка в МойСклад

    def test_retail_and_off(self):
        self.assertEqual(self.order(qty=1, me="cid-9", mode="retail")[1], 200)   # розница — без минимума
        oh.set_opt_min(0)
        self.assertEqual(self.order(qty=1)[1], 200)

    def test_catalog_and_admin(self):
        self.patch(oh, "live", lambda max_age=None: {"updated": "x", "items": [dict(ITEM)]})
        self.patch(admin, "is_admin", lambda: True)
        self.addCleanup(lambda: [oh._cache.pop(k) for k in list(oh._cache) if isinstance(k, tuple) and k[0] == "catjson"])
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        app.register_blueprint(admin.bp)
        c = app.test_client()
        self.assertEqual(json.loads(c.get("/catalog").data)["minOrder"], 20000)
        self.assertNotIn("minOrder", json.loads(c.get("/catalog?mode=retail").data))
        r = json.loads(c.put("/admin/api/sites", json={"optMin": 15000}).data)
        self.assertEqual(r["optMin"], 15000)
        self.assertEqual(json.loads(c.get("/catalog").data)["minOrder"], 15000)


if __name__ == "__main__":
    unittest.main()
