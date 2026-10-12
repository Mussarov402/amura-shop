"""Что дозаказать (по зеркалу) и заказ поставщику через складские операции (двойная запись)."""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401
import test_whops as tw  # noqa: E402
import mirror  # noqa: E402
import reorder  # noqa: E402


class ReorderTest(unittest.TestCase):
    def setUp(self):
        tw.WhOpsTest.setUp(self)
        self.ms.docs["purchaseorder"] = []
        t = datetime.now(mirror.MS_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with mirror.db() as d:
            for x in ("ms_doc", "ms_doc_line", "ms_stock"):
                d.run(f"DELETE FROM {x}")
            d.run("INSERT INTO ms_product (id, kind, code, name, buy_price, archived, deleted) VALUES ('p2', 'product', '3', 'Shampoo', 500, 0, 0)")
            for i, ty, ag in (("d1", "demand", "k1"), ("r1", "retaildemand", ""), ("sp1", "supply", "k1")):
                d.run("INSERT INTO ms_doc (id, type, number, moment, agent_id, sum, applicable, deleted) VALUES (%s, %s, '1', %s, %s, 0, 1, 0)", (i, ty, t, ag))
            # p1 продано 28 за 28 дней (1 в день), остаток 5 → меньше 14 дней → дозаказать до 30 дней: 30 − 5 = 25
            # p2 продано 2 (0,07 в день), остаток 10 → хватит надолго
            for doc, pid, q in (("d1", "p1", 20), ("r1", "p1", 8), ("d1", "p2", 2), ("sp1", "p1", 50)):
                d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, discount, sum) VALUES (%s, %s, %s, %s, 0, 0, 0)", (doc, doc + pid, pid, q))
            d.run("INSERT INTO ms_stock (product_id, store_id, stock, reserve, synced) VALUES ('p1', 'sA', 5, 0, 0)")
            d.run("INSERT INTO ms_stock (product_id, store_id, stock, reserve, synced) VALUES ('p2', 'sA', 10, 0, 0)")

    def post(self, **b):
        return self.c.post("/admin/api/warehouse/ops", headers=self.h, json=b)

    def test_suggest(self):
        r = self.c.get("/admin/api/warehouse/reorder?days=28&cover=14&target=30", headers=self.h).get_json()
        self.assertEqual([(x["id"], x["qty"], x["daysLeft"], x["agent"], x["price"]) for x in r["rows"]], [("p1", 25, 5.0, "k1", 1200)])
        self.assertEqual(r["suppliers"], [{"agent": "k1", "name": "KorShop", "count": 1, "sum": 30000}])
        self.assertEqual(reorder.suggest(28, 14, 30, "Sham")["rows"], [])

    def test_purchase_order(self):
        r = self.post(kind="purchaseorder", store="sA", agent="k1", lines=[{"id": "p1", "qty": 25, "price": 1150}]).get_json()["op"]
        self.assertEqual((r["kind"], r["status"]), ("purchaseorder", "sent"))
        doc = self.ms.docs["purchaseorder"][0]
        self.assertTrue(doc["agent"]["meta"]["href"].endswith("/counterparty/k1"))
        self.assertEqual([(p["quantity"], p["price"]) for p in doc["positions"]], [(25, 115000)])
        self.assertEqual(self.post(kind="purchaseorder", store="sA", lines=[{"id": "p1", "qty": 1}]).status_code, 400)   # без поставщика


if __name__ == "__main__":
    unittest.main()
