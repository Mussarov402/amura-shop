"""Аналитика карточек: пачка событий с розничного сайта → счётчики по дням; одна сессия = одно посещение; боты не считаются;
заказы — строки без дублей; воронка и товары за период, фильтр по источнику; модуль выключен — ничего не пишем и раздела нет."""
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
import analytics as an  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Safari/604.1"


class Analytics(unittest.TestCase):
    def setUp(self):
        with an.db() as d:
            for t in ("an_sess", "an_prod", "an_oline"):
                d.run(f"DELETE FROM {t}")
        modules.update("analytics", on=True)
        old = oh.panel_catalog
        oh.panel_catalog = lambda: {"p1": {"id": "p1", "name": "Крем", "brand": "Anua", "img": "img/p1.webp"}}
        self.addCleanup(lambda: setattr(oh, "panel_catalog", old))
        old2 = admin.is_admin
        admin.is_admin = lambda: True
        self.addCleanup(lambda: setattr(admin, "is_admin", old2))
        app = Flask(__name__)
        app.register_blueprint(an.bp)
        self.c = app.test_client()

    def send(self, sid, ev, src="", ua=UA):
        return self.c.post("/an/ev", data=json.dumps({"sid": sid, "src": src, "ev": ev}), headers={"User-Agent": ua, "Content-Type": "text/plain"})

    def test_funnel_and_products(self):
        self.assertEqual(self.send("aaaaaaaa1", [["s"], ["v", "p1"], ["v", "p2"], ["c", "p1"]]).status_code, 204)
        self.send("aaaaaaaa1", [["k"]])                                            # та же сессия — то же посещение
        self.send("bbbbbbbb2", [["s"], ["v", "p1"]], src="aigerim")
        self.send("cccccccc3", [["s"]])
        self.send("dddddddd4", [["s"], ["v", "p1"]], ua="Googlebot/2.1")            # бот — не считаем
        self.send("BAD", [["s"]])                                                  # чужой формат номера — не считаем
        an.record_order("1800", [{"id": "p1", "qty": 2, "price": 3000}], "")
        an.record_order("1800", [{"id": "p1", "qty": 2, "price": 3000}], "")      # повтор — без дубля
        r = an.report(1)
        self.assertEqual(r["funnel"], {"visits": 3, "viewed": 2, "carted": 1, "checkout": 1, "paid": 1, "revenue": 6000})
        p1 = next(x for x in r["rows"] if x["id"] == "p1")
        self.assertEqual((p1["views"], p1["carts"], p1["qty"], p1["sum"], p1["toCart"], p1["name"]), (2, 1, 2, 6000, 50.0, "Крем"))
        self.assertTrue(p1["img"].endswith("/img/p1.webp"))
        self.assertEqual(an.report(1, src="aigerim")["funnel"]["visits"], 1)
        self.assertEqual(r["sources"], ["aigerim"])
        self.assertTrue(self.c.get("/admin/api/analytics?days=7").get_json()["ok"])

    def test_off(self):
        modules.update("analytics", on=False)
        self.addCleanup(lambda: modules.update("analytics", on=True))
        self.send("aaaaaaaa1", [["s"], ["v", "p1"]])
        an.record_order("1801", [{"id": "p1", "qty": 1, "price": 100}], "")
        with an.db() as d:
            self.assertEqual(d.run("SELECT COUNT(*) FROM an_sess", one=True)[0] + d.run("SELECT COUNT(*) FROM an_oline", one=True)[0], 0)
        self.assertEqual(self.c.get("/admin/api/analytics").status_code, 404)


if __name__ == "__main__":
    unittest.main()
