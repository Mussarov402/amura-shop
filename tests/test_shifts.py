"""Кассовые смены: загрузка в зеркало (только чтение) и просмотр смен с чеками в «Отчётах»."""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401
import admin  # noqa: E402
import app  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402


def ms_t(h=0):
    return (datetime.now(mirror.MS_TZ) - timedelta(hours=h)).strftime("%Y-%m-%d %H:%M:%S")


def ref(kind, i):
    return {"meta": {"href": f"{oh.API}/entity/{kind}/{i}"}}


class ShiftsTest(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + admin.make_admin_token()}
        self.calls = []
        with mirror.db() as d:
            for x in ("ms_doc", "ms_doc_line", "ms_shift", "ms_store", "ms_agent"):
                d.run(f"DELETE FROM {x}")
            d.run("DELETE FROM ms_sync WHERE entity='retailshift'")
            d.run("DELETE FROM setting WHERE key='mod_finance'")
            d.run("INSERT INTO ms_store (id, name, archived) VALUES ('sA', 'Основной склад', 0)")
            d.run("INSERT INTO ms_agent (id, name, archived, deleted) VALUES ('k1', 'Розничный покупатель', 0, 0)")

    def fake(self, rows):
        def ms(method, path, params=None, json=None, timeout=None):
            self.calls.append((method, path))
            assert method == "GET"
            return {"rows": rows if not (params or {}).get("offset") else []}
        oh.ms = ms

    def load(self):
        """Две смены: закрытая (6–2 ч назад) и открытая (с часа назад); суммы МойСклад — в тиынах."""
        a, b, c = ms_t(6), ms_t(2), ms_t(1)
        self.fake([
            {"id": "s1", "name": "00101", "moment": a, "closeDate": b, "proceedsCash": 1500000, "proceedsNoCash": 500000,
             "receivedCash": 1500000, "receivedNoCash": 500000, "bankComission": 0, "store": ref("store", "sA"),
             "retailStore": ref("retailstore", "rp"), "updated": b},
            {"id": "s2", "name": "00102", "moment": c, "proceedsCash": 300000, "proceedsNoCash": 0, "store": ref("store", "sA"), "updated": c},
        ])
        n, caught = mirror.sync_entity("retailshift", pause=0)
        self.assertEqual((n, caught), (2, True))
        docs = [("r1", "retaildemand", ms_t(5), "sA", 12000, 1), ("r2", "retaildemand", ms_t(3), "sA", 8000, 1),
                ("rr", "retailsalesreturn", ms_t(3), "sA", 1000, 1), ("r3", "retaildemand", ms_t(0.5), "sA", 3000, 1),
                ("x1", "retaildemand", ms_t(5), "sB", 999, 1),            # другой склад — не эта смена
                ("x2", "retaildemand", ms_t(5), "sA", 777, 0),            # не проведён
                ("x3", "retaildemand", ms_t(1.5), "sA", 555, 1)]          # между сменами
        with mirror.db() as d:
            for i, t, mo, st, sm, ap in docs:
                d.run("INSERT INTO ms_doc (id, type, number, moment, agent_id, store_id, sum, applicable, deleted) VALUES (%s, %s, %s, %s, 'k1', %s, %s, %s, 0)",
                      (i, t, i, mo, st, sm, ap))
            d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, discount, sum) VALUES ('r1', 'l1', 'p1', 2, 6000, 0, 12000)")

    def get(self, path):
        return self.c.get("/admin/api/reports/" + path, headers=self.h)

    def test_mirror_reads_only(self):
        self.load()
        self.assertTrue(all(m == "GET" and p == "/entity/retailshift" for m, p in self.calls))
        with mirror.db() as d:
            r = d.run("SELECT number, cash, nocash, close_at, store_id, point FROM ms_shift WHERE id='s1'", one=True)
        self.assertEqual((r[0], r[1], r[2], r[4], r[5]), ("00101", 15000, 5000, "sA", "rp"))
        self.assertTrue(r[3])
        self.assertIn("retailshift", mirror.ENTITIES)

    def test_list(self):
        self.load()
        r = self.get("shifts?days=7").get_json()
        self.assertTrue(r["ok"])
        self.assertEqual((r["count"], r["open"]), (2, 1))
        self.assertEqual(r["total"]["total"], 23000)
        self.assertEqual((r["total"]["cash"], r["total"]["noCash"]), (18000, 5000))
        s2, s1 = r["rows"]                                                 # свежая смена — первой
        self.assertEqual((s1["number"], s1["receipts"], s1["returns"], s1["store"]), ("00101", 2, 1, "Основной склад"))
        self.assertEqual((s2["close"], s2["receipts"]), ("", 1))

    def test_one_shift(self):
        self.load()
        s = self.get("shifts/s1").get_json()["shift"]
        self.assertEqual(sorted(x["id"] for x in s["docs"]), ["r1", "r2", "rr"])
        self.assertEqual(s["docs"][-1]["id"], "r1")                       # новые чеки — сверху
        self.assertEqual((s["receipts"], s["receiptsSum"], s["returnsSum"]), (2, 20000, 1000))
        self.assertEqual({x["id"]: x["lines"] for x in s["docs"]}["r1"], 1)
        self.assertEqual(self.get("shifts/nope").status_code, 404)

    def test_owner_and_module(self):
        self.load()
        self.assertEqual(self.c.get("/admin/api/reports/shifts").status_code, 401)
        with mirror.db() as d:
            d.run("INSERT INTO setting (key, value) VALUES ('mod_finance', '0')")
        self.assertEqual(self.get("shifts").status_code, 404)


if __name__ == "__main__":
    unittest.main()
