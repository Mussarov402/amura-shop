"""Окно заказа в панели: остаток по позициям (как в МойСклад) и кнопка «Резерв / Снять резерв»."""
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

OID, STORE, P1, P2 = "0" * 8 + "-0000-0000-0000-" + "0" * 12, "1" * 8 + "-1111-1111-1111-" + "1" * 12, \
    "a" * 8 + "-aaaa-aaaa-aaaa-" + "a" * 12, "b" * 8 + "-bbbb-bbbb-bbbb-" + "b" * 12


def meta(t, i):
    return {"meta": {"type": t, "href": f"https://x/entity/{t}/{i}"}, "id": i, "name": "Крем" if t == "product" else oh.FEE_NAME, "code": "1"}


class OrderReserve(unittest.TestCase):
    def setUp(self):
        self.puts = []
        self.res = 3
        p = lambda: [{"id": "pos1", "quantity": 3, "price": 500000, "reserve": self.res, "discount": 0, "assortment": meta("product", P1)},  # noqa: E731
                     {"id": "pos2", "quantity": 1, "price": 4750, "assortment": meta("service", P2)}]
        order = {"id": OID, "name": "1600", "sum": 1504750, "agent": {"name": "Аружан"}, "state": {"name": "Новый"},
                 "store": {"meta": {"href": f"https://x/entity/store/{STORE}"}}, "moment": "2026-10-08 10:00:00"}

        def ms(method, path, **kw):
            if method == "PUT":
                self.puts.append(kw["json"])
                return {}
            if path == "/report/stock/bystore/current":
                k = kw["params"]["stockType"]
                return [{"assortmentId": P1, "storeId": STORE, k: 10 if k == "stock" else 4},
                        {"assortmentId": P1, "storeId": "other", k: 99}]
            raise AssertionError(path)
        self.patch(oh, "ms", ms)
        self.patch(admin, "_order_fetch", lambda n: (order, p()))
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Т"})
        self.patch(admin, "_olog", lambda *a, **k: None)
        for k in [k for k in oh._cache if k.startswith("ostk:")]:
            oh._cache.pop(k)
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def test_stock_on_order_store(self):
        r = json.loads(self.c.get(f"/admin/api/orders/1600/stock?store={STORE}&ids={P1}").data)
        self.assertEqual(r["items"][P1], {"stock": 10, "reserve": 4, "free": 6})       # чужой склад не считается

    def test_toggle_reserve(self):
        r = self.c.post("/admin/api/orders/1600/reserve", data=json.dumps({"on": False}), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        pos = self.puts[-1]["positions"]
        self.assertEqual(pos[0]["reserve"], 0)
        self.assertNotIn("reserve", pos[1])                                           # услуга не резервируется
        self.assertEqual(pos[0]["discount"], 0)                                       # скидка сохраняется
        self.c.post("/admin/api/orders/1600/reserve", data=json.dumps({"on": True}), content_type="application/json")
        self.assertEqual(self.puts[-1]["positions"][0]["reserve"], 3)

    def test_edit_keeps_reserve_off(self):
        self.res = 0
        self.patch(oh, "pdf_prepare", lambda *a: None)
        body = {"lines": [{"id": P1, "type": "product", "qty": 2, "price": 5000, "pos": "pos1"}]}
        self.assertEqual(self.c.put("/admin/api/orders/1600", data=json.dumps(body), content_type="application/json").status_code, 200)
        self.assertNotIn("reserve", self.puts[-1]["positions"][0])                    # резерв сняли — правка его не ставит
        self.res = 3
        self.c.put("/admin/api/orders/1600", data=json.dumps(body), content_type="application/json")
        self.assertEqual(self.puts[-1]["positions"][0]["reserve"], 2)


if __name__ == "__main__":
    unittest.main()
