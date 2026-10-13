"""Возврат покупателя по отгрузке: склад, контрагент и цены из отгрузки, не больше отгруженного, без дублей (SQLite, подменённый oh.ms)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401
import test_whops as tw  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402
import payments  # noqa: E402
import whops  # noqa: E402


class ReturnsTest(unittest.TestCase):
    def setUp(self):
        tw.WhOpsTest.setUp(self)
        self.ms.docs["salesreturn"] = []
        self.ms.docs["purchasereturn"] = []
        self.refreshed = []
        payments._bg = lambda fn, *a: self.refreshed.append(a)
        with mirror.db() as d:
            for t in ("ms_doc", "ms_doc_line"):
                d.run(f"DELETE FROM {t}")
            d.run("INSERT INTO ms_doc (id, type, number, moment, agent_id, store_id, sum, applicable, deleted) VALUES ('dm1', 'demand', '15', '2026-10-10 10:00:00', 'k1', 'sA', 3300, 1, 0)")
            d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, discount, sum) VALUES ('dm1', 'l1', 'p1', 2, 1500, 10, 2700)")
            d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, discount, sum) VALUES ('dm1', 'l2', 'v1', 1, 600, 0, 600)")
            d.run("INSERT INTO ms_doc (id, type, number, moment, agent_id, store_id, sum, applicable, deleted) VALUES ('dm0', 'demand', '16', '2026-10-10 10:00:00', 'k1', 'sA', 1, 0, 0)")
            d.run("INSERT INTO ms_doc (id, type, number, moment, agent_id, store_id, sum, applicable, deleted) VALUES ('sp1', 'supply', '42', '2026-10-10 10:00:00', 'k1', 'sB', 12000, 1, 0)")
            d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, discount, sum) VALUES ('sp1', 'l1', 'p1', 10, 1200, 0, 12000)")

    def ret(self, **b):
        return self.c.post("/admin/api/returns", headers=self.h, json=b)

    def test_return_flow(self):
        dm = self.c.get("/admin/api/returns/dm1", headers=self.h).get_json()["demand"]
        self.assertEqual([(l["id"], l["qty"], l["price"], l["left"]) for l in dm["lines"]], [("p1", 2, 1350, 2), ("v1", 1, 600, 1)])
        r = self.ret(demand="dm1", lines=[{"id": "p1", "qty": 1}, {"id": "v1", "qty": 0}], descr="брак").get_json()["op"]
        self.assertEqual((r["kind"], r["status"], r["agent"], r["store"]), ("salesreturn", "sent", "k1", "sA"))
        doc = self.ms.docs["salesreturn"][0]
        self.assertTrue(doc["demand"]["meta"]["href"].endswith("/entity/demand/dm1"))
        self.assertTrue(doc["agent"]["meta"]["href"].endswith("/entity/counterparty/k1"))
        self.assertEqual([(p["quantity"], p["price"]) for p in doc["positions"]], [(1, 135000)])        # цена со скидкой, в копейках
        self.assertEqual(self.refreshed, [("k1",)])                                                    # долг клиента перечитан
        left = {l["id"]: l["left"] for l in self.c.get("/admin/api/returns/dm1", headers=self.h).get_json()["demand"]["lines"]}
        self.assertEqual(left, {"p1": 1, "v1": 1})
        self.assertEqual(self.ret(demand="dm1", lines=[{"id": "p1", "qty": 2}]).status_code, 400)       # больше, чем осталось
        self.assertEqual(len(self.ms.docs["salesreturn"]), 1)

    def test_validation_and_access(self):
        self.assertEqual(self.ret(demand="dm0", lines=[{"id": "p1", "qty": 1}]).status_code, 400)       # не проведена
        self.assertEqual(self.ret(demand="dm1", lines=[{"id": "p2", "qty": 1}]).status_code, 400)       # не из отгрузки
        self.assertEqual(self.ret(demand="dm1", lines=[{"id": "p1", "qty": 0}]).status_code, 400)
        self.assertEqual(self.c.get("/admin/api/returns/zzz", headers=self.h).status_code, 404)
        self.assertEqual(self.c.post("/admin/api/returns", json={"demand": "dm1"}).status_code, 401)
        # через складские операции (право «Склад») возврат не оформить
        r = self.c.post("/admin/api/warehouse/ops", headers=self.h, json={"kind": "salesreturn", "store": "sA", "lines": [{"id": "p1", "qty": 1}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.ms.docs["salesreturn"], [])

    def test_purchase_return(self):
        dm = self.c.get("/admin/api/returns/sp1?type=supply", headers=self.h).get_json()["demand"]
        self.assertEqual([(l["id"], l["qty"], l["price"]) for l in dm["lines"]], [("p1", 10, 1200)])
        self.assertEqual(self.c.get("/admin/api/returns/sp1", headers=self.h).status_code, 404)           # приёмка — не отгрузка
        r = self.ret(kind="purchasereturn", demand="sp1", lines=[{"id": "p1", "qty": 3}], descr="брак").get_json()["op"]
        self.assertEqual((r["kind"], r["status"], r["store"], r["agent"]), ("purchasereturn", "sent", "sB", "k1"))
        doc = self.ms.docs["purchasereturn"][0]
        self.assertTrue(doc["supply"]["meta"]["href"].endswith("/entity/supply/sp1"))
        self.assertNotIn("demand", doc)
        self.assertEqual([(p["quantity"], p["price"]) for p in doc["positions"]], [(3, 120000)])
        self.assertEqual(self.ret(kind="purchasereturn", demand="sp1", lines=[{"id": "p1", "qty": 8}]).status_code, 400)   # осталось 7
        self.assertEqual(self.ret(kind="purchasereturn", demand="dm1", lines=[{"id": "p1", "qty": 1}]).status_code, 400)   # отгрузка — не приёмка
        self.assertEqual(self.ms.docs["salesreturn"], [])

    def test_returns_made_in_moysklad_count(self):
        """Возврат, сделанный прямо в МойСклад (зеркало знает основание), уменьшает «можно вернуть»; наш — не считается дважды."""
        import mirror as m
        parse = m._doc_parser("salesreturn")
        row = parse({"meta": {"href": "https://x/entity/salesreturn/sr9"}, "id": "sr9", "name": "9", "moment": "2026-10-11 10:00:00",
                     "applicable": True, "demand": {"meta": {"href": "https://x/entity/demand/dm1"}}}, 0)
        self.assertEqual(row[m._DCOLS.split(", ").index("base_id")], "dm1")                                                              # основание из МойСклад
        self.assertIsNone(m._doc_parser("demand")({"id": "x", "meta": {"href": "https://x/entity/demand/x"}}, 0)[m._DCOLS.split(", ").index("base_id")])
        with mirror.db() as d:
            d.run("INSERT INTO ms_doc (id, type, number, moment, agent_id, store_id, sum, applicable, deleted, base_id) VALUES ('sr9', 'salesreturn', '9', '2026-10-11 10:00:00', 'k1', 'sA', 1350, 1, 0, 'dm1')")
            d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, discount, sum) VALUES ('sr9', 'l1', 'p1', 1, 1350, 0, 1350)")
        left = {l["id"]: l["left"] for l in whops.demand_lines("dm1")["lines"]}
        self.assertEqual(left["p1"], 1)
        self.assertEqual(self.ret(demand="dm1", lines=[{"id": "p1", "qty": 2}]).status_code, 400)     # второй раз не вернуть
        r = self.ret(demand="dm1", lines=[{"id": "p1", "qty": 1}]).get_json()["op"]
        with mirror.db() as d:                                                                        # наш возврат пришёл в зеркало
            d.run("INSERT INTO ms_doc (id, type, number, moment, agent_id, store_id, sum, applicable, deleted, base_id) VALUES (%s, 'salesreturn', '10', '2026-10-11 11:00:00', 'k1', 'sA', 1350, 1, 0, 'dm1')", (r["msId"],))
            d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, discount, sum) VALUES (%s, 'l1', 'p1', 1, 1350, 0, 1350)", (r["msId"],))
        self.assertEqual({l["id"]: l["returned"] for l in whops.demand_lines("dm1")["lines"]}["p1"], 2)   # 1 из МойСклад + 1 наш, не 3

    def test_lost_response_no_duplicate(self):
        self.ms.fail = ["lost"]
        r = self.ret(demand="dm1", lines=[{"id": "p1", "qty": 1}]).get_json()["op"]
        self.assertEqual(r["status"], "queued")
        whops.retry(r["id"])
        self.assertEqual(len(self.ms.docs["salesreturn"]), 1)


if __name__ == "__main__":
    unittest.main()
