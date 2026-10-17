"""«Хиты продаж» розницы: продажи за 30 дней (розница ×1, отгрузки опта ×0,2, оптовые заказы не дважды), только в наличии и не скрытые,
закреплённые первыми, исключённые убраны; в живом каталоге розницы — hits и pop, у опта — нет."""
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402
import admin  # noqa: E402
import hits  # noqa: E402
import inbox  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone(timedelta(hours=3)))
ITEMS = [{"id": p, "name": "Товар " + p, "qty": 5, "opt": 1000, "rtl": 1500, "img": f"img/{p}.webp"} for p in ("a", "b", "c", "d", "e")]


class Hits(unittest.TestCase):
    def setUp(self):
        with mirror.db() as d:
            d.run("DELETE FROM ms_doc")
            d.run("DELETE FROM ms_doc_line")
            docs = [("r1", "customerorder", oh.RETAIL_MARK, "2026-10-15 10:00:00", [("a", 3), ("b", 1)]),
                    ("r2", "retaildemand", "", "2026-10-16 10:00:00", [("b", 1)]),
                    ("w1", "customerorder", "Заказ с сайта", "2026-10-16 10:00:00", [("c", 50)]),   # опт: заказ не считаем
                    ("w2", "demand", "", "2026-10-16 10:00:00", [("c", 50), ("e", 1)]),            # опт: отгрузка ×0,2
                    ("old", "customerorder", oh.RETAIL_MARK, "2026-08-01 10:00:00", [("d", 100)]),   # старше 30 дней
                    ("hid", "retaildemand", "", "2026-10-16 10:00:00", [("x", 99)])]               # не на сайте
            for did, typ, descr, moment, lines in docs:
                d.run("INSERT INTO ms_doc (id, type, number, moment, descr, sum, deleted) VALUES (%s,%s,%s,%s,%s,0,0)", (did, typ, did, moment, descr))
                for i, (pid, q) in enumerate(lines):
                    d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, sum) VALUES (%s,%s,%s,%s,0,0)", (did, str(i), pid, q))
        with inbox.db() as d:
            d.run("DELETE FROM setting WHERE key IN ('hits_pin', 'hits_off')")
        self.patch(oh, "live", lambda max_age=None: {"updated": "x", "items": [dict(i) for i in ITEMS], "hiddenItems": [], "outItems": []})
        self.patch(hits, "sales", (lambda real: lambda days=30, now=None: real(days, NOW))(hits.sales))
        hits.reset()
        self.addCleanup(hits.reset)

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def test_rank(self):
        s = hits.sales()
        self.assertEqual((s["a"], s["b"], s["c"], round(s["e"], 1)), (3, 2, 10, 0.2))
        self.assertNotIn("d", s)
        self.assertEqual(hits.compute()["hits"], ["c", "a", "b", "e"])      # «x» нет на сайте

    def test_pin_off_and_catalog(self):
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Владелец"})
        app = Flask(__name__)
        app.register_blueprint(hits.bp)
        app.register_blueprint(oh.bp)
        c = app.test_client()
        c.post("/admin/api/hits", json={"id": "d", "act": "pin"})
        r = c.post("/admin/api/hits", json={"id": "c", "act": "off"}).get_json()
        self.assertEqual([x["id"] for x in r["hits"]], ["d", "a", "b", "e"])
        self.assertTrue(r["hits"][0]["pin"])
        self.assertEqual([x["id"] for x in r["off"]], ["c"])
        self.patch(oh, "site_on", lambda kind: True)
        self.patch(oh, "is_wholesale", lambda cid: False)
        self.patch(oh, "opt_min", lambda: 0)
        cat = json.loads(c.get("/catalog?mode=retail").data)
        self.assertEqual(cat["hits"][:2], ["d", "a"])
        self.assertIn("c", cat["pop"])                                     # из «Хитов» убран, но в «Популярных» остаётся
        self.assertNotIn("hits", json.loads(c.get("/catalog").data))       # опт — без хитов
        c.post("/admin/api/hits", json={"id": "c", "act": "on"})
        self.assertEqual(hits.public()["hits"][1], "c")


if __name__ == "__main__":
    unittest.main()
