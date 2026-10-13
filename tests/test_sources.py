"""Модуль «Источники»: выключен — ни на что не влияет; метка и промокод в заказе, переходы, скидка только по настройке, отчёт."""
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
import modules  # noqa: E402
import order_hook as oh  # noqa: E402
import sources  # noqa: E402

ITEM = {"id": "p1", "name": "Крем", "qty": 50, "opt": 5000, "mid": 4900, "box": 4800, "boxQty": 40, "rtl": 6690}


class Sources(unittest.TestCase):
    def setUp(self):
        with sources.db() as d:
            for t in ("src_source", "src_event", "src_order"):
                d.run(f"DELETE FROM {t}")
        sources.reset()
        modules.update("sources", on=True)
        self.addCleanup(lambda: modules.update("sources", on=False))
        self.posted = []
        for name, val in {"catalog": lambda: {"p1": dict(ITEM)}, "too_many": lambda *a, **k: False,
                          "find_or_create_agent": lambda *a: "agent-1", "organization": lambda: "org-1", "store_id": lambda: "store-1",
                          "fee_service_id": lambda: "fee-1", "delivery_service_id": lambda: "dlv-1", "notify_bg": lambda fn: None,
                          "is_wholesale": lambda cid: True,
                          "_post_order": lambda body: self.posted.append(body) or {"name": str(1500 + len(self.posted)), "id": "o" + str(len(self.posted))}}.items():
            self.patch(oh, name, val)
        self.patch(sources, "_cancelled_ids", lambda ids: set())
        self.patch(admin, "is_admin", lambda: True)
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        app.register_blueprint(sources.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def add(self, **kw):
        b = {"name": "Айгерим", "code": "aigerim", "cost": 50000} | kw
        return self.c.post("/admin/api/sources", data=json.dumps(b), content_type="application/json")

    def order(self, **extra):
        d = {"name": "Тамирис", "phone": "+77012345678", "city": "Алматы", "shipping": "pickup", "items": [{"id": "p1", "qty": 2}]} | extra
        return oh.order_core(d, "k" + str(len(self.posted)) + json.dumps(extra), "1.1.1.1", None)

    def test_off_changes_nothing(self):
        self.add(promo="AIG10", discount=10)
        modules.update("sources", on=False)
        res, st = self.order(ref="aigerim", promo="AIG10")
        self.assertEqual(st, 200)
        self.assertEqual(self.posted[-1]["positions"][0]["price"], 500000)           # цена без скидки
        self.assertNotIn("Источник", self.posted[-1]["description"])
        self.assertEqual(self.c.post("/ref/visit", json={"ref": "aigerim"}).status_code, 404)
        self.assertEqual(self.c.get("/admin/api/sources").status_code, 404)
        with sources.db() as d:
            self.assertEqual(d.run("SELECT COUNT(*) FROM src_order", one=True)[0], 0)

    def test_ref_order_and_report(self):
        self.assertEqual(self.add().status_code, 200)
        self.assertEqual(self.add(code="Плохой код").status_code, 400)
        self.assertEqual(self.c.post("/ref/visit", json={"ref": "aigerim"}).status_code, 200)
        self.c.post("/ref/visit", json={"ref": "aigerim", "kind": "cart"})
        self.assertEqual(self.c.post("/ref/visit", json={"ref": "nobody"}).status_code, 404)   # только существующие коды
        res, st = self.order(ref="aigerim")
        self.assertEqual(st, 200)
        self.assertEqual(self.posted[-1]["positions"][0]["price"], 500000)           # метка без промокода — цена прежняя
        self.assertIn("Источник: aigerim", self.posted[-1]["description"])
        self.order()                                                                 # без источника
        r = json.loads(self.c.get("/admin/api/sources?days=30").data)["report"]
        a = next(x for x in r["rows"] if x["code"] == "aigerim")
        self.assertEqual((a["visits"], a["carts"], a["orders"], a["revenue"], a["conv"], a["avg"]), (1, 1, 1, 10000, 100.0, 10000))
        self.assertEqual(a["payback"], 0.2)
        self.assertEqual(a["top"][0]["qty"], 2)
        none = next(x for x in r["rows"] if x["code"] == "")
        self.assertEqual(none["orders"], 1)
        self.patch(sources, "_cancelled_ids", lambda ids: {"o1"})                    # отменённый в МойСклад не считается
        r = json.loads(self.c.get("/admin/api/sources?days=30").data)["report"]
        a = next(x for x in r["rows"] if x["code"] == "aigerim")
        self.assertEqual((a["orders"], a["revenue"], a["cancelled"]), (0, 0, 1))

    def test_promo(self):
        self.add(promo="aig10")
        self.add(name="Реклама", code="ads")
        self.assertEqual(json.loads(self.c.get("/ref/promo?code=AIG10").data)["discount"], 0)
        res, st = self.order(ref="ads", promo="AIG10")
        self.assertEqual(self.posted[-1]["positions"][0]["price"], 500000)           # скидка 0 — цена прежняя
        self.assertIn("Источник: aigerim", self.posted[-1]["description"])           # промокод важнее метки
        self.add(promo="AIG10", discount=10)
        sources.reset()
        res, st = self.order(promo="aig10")
        self.assertEqual(self.posted[-1]["positions"][0]["price"], 450000)           # скидка задана владельцем — пересчёт на сервере
        self.assertIn("скидка 10%", self.posted[-1]["description"])
        self.assertEqual(self.add(name="Другой", code="other", promo="AIG10").status_code, 400)   # промокод не повторяется
        self.assertEqual(self.c.get("/ref/promo?code=NOPE").status_code, 404)

    def test_visit_rate_limit(self):
        self.add()
        self.patch(oh, "too_many", lambda key, limit=5, window=600: True)
        self.assertEqual(self.c.post("/ref/visit", json={"ref": "aigerim"}).status_code, 429)

    def test_export(self):
        self.add()
        r = json.loads(self.c.post("/admin/api/sources/export", json={"days": 7}).data)
        self.assertTrue(r["url"].startswith("/admin/report-files/"))
        self.assertTrue(self.c.get(r["url"]).data.startswith(b"PK"))


if __name__ == "__main__":
    unittest.main()
