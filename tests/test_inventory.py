"""Склад: инвентаризация — черновик у нас, расхождения проводятся оприходованием/списанием без дублей (SQLite, подменённый oh.ms)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401
import test_whops as tw  # noqa: E402
import admin  # noqa: E402
import app  # noqa: E402
import inventory  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402
import requests  # noqa: E402
import whops  # noqa: E402


class FakeInvMS(tw.FakeMSW):
    """Плюс отчёт текущих остатков; book — {(товар, склад): остаток}, нулевых строк в отчёте нет (как в МойСклад)."""
    def __init__(self):
        super().__init__()
        self.book, self.report_down = {("p1", "sA"): 10, ("v1", "sA"): 3, ("p1", "sB"): 99}, False

    def __call__(self, method, path, params=None, json=None, **kw):
        if path == "/report/stock/bystore/current":
            self.calls.append((method, path, params, json))
            if self.report_down:
                raise requests.ConnectionError("timeout")
            return [{"assortmentId": a, "storeId": s, "stock": q} for (a, s), q in self.book.items() if q]
        return super().__call__(method, path, params=params, json=json, **kw)


class InventoryTest(unittest.TestCase):
    def setUp(self):
        tw.WhOpsTest.setUp(self)
        self.ms = oh.ms = FakeInvMS()
        with mirror.db() as d:
            d.run("INSERT INTO ms_product (id, kind, code, name, buy_price, archived, deleted) VALUES ('p2', 'product', '3', 'Шампунь', 500, 0, 0)")
        with inventory.db() as d:
            d.run("DELETE FROM wh_inv")

    def api(self, m, path, **b):
        return getattr(self.c, m)("/admin/api/warehouse/inventory" + path, headers=self.h, json=b or None)

    def test_flow(self):
        inv = self.api("post", "", store="sA").get_json()["inv"]
        self.assertEqual(inv["status"], "draft")
        # p1: учёт 10, факт 8 → недостача 2; v1: учёт 3, факт 3; p2: учёта нет (0), факт 4 → излишек 4
        r = self.api("put", "/" + inv["id"], lines=[{"id": "p1", "fact": 8}, {"id": "v1", "fact": 3}, {"id": "p2", "fact": 4}, {"id": "p1", "fact": 1}])
        self.assertEqual([x["fact"] for x in r.get_json()["inv"]["lines"]], [8, 3, 4])            # повтор товара не задваивается
        self.assertEqual(self.ms.calls, [])                                                     # черновик — в МойСклад ничего
        r = self.api("post", f"/{inv['id']}/conduct").get_json()["inv"]
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["enter"]["status"], "sent")
        self.assertEqual(r["loss"]["status"], "sent")
        en, lo = self.ms.docs["enter"][0], self.ms.docs["loss"][0]
        self.assertEqual([(p["assortment"]["meta"]["href"].rsplit("/", 1)[-1], p["quantity"], p["price"]) for p in en["positions"]], [("p2", 4, 50000)])
        self.assertEqual([(p["assortment"]["meta"]["href"].rsplit("/", 1)[-1], p["quantity"], p["price"]) for p in lo["positions"]], [("p1", 2, 120000)])
        self.assertIn("Инвентаризация", en["description"])
        self.assertEqual(self.api("put", "/" + inv["id"], lines=[]).status_code, 400)            # проведённую не правят
        self.api("post", f"/{inv['id']}/conduct")                                               # повтор — ничего нового
        self.assertEqual((len(self.ms.docs["enter"]), len(self.ms.docs["loss"])), (1, 1))
        lst = self.c.get("/admin/api/warehouse/ops", headers=self.h).get_json()
        self.assertEqual(lst["inv"][0]["id"], inv["id"])

    def test_no_diff_no_docs(self):
        inv = self.api("post", "", store="sA").get_json()["inv"]
        self.api("put", "/" + inv["id"], lines=[{"id": "p1", "fact": 10}])
        r = self.api("post", f"/{inv['id']}/conduct").get_json()["inv"]
        self.assertEqual((r["status"], r["enter"], r["loss"]), ("done", None, None))
        self.assertEqual(self.ms.docs["enter"] + self.ms.docs["loss"], [])

    def test_report_down_stays_draft(self):
        inv = self.api("post", "", store="sA").get_json()["inv"]
        self.api("put", "/" + inv["id"], lines=[{"id": "p1", "fact": 8}])
        self.ms.report_down = True
        r = self.api("post", f"/{inv['id']}/conduct")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(inventory.get(inv["id"])["status"], "draft")                          # можно править и провести позже
        self.ms.report_down = False
        self.assertEqual(self.api("post", f"/{inv['id']}/conduct").get_json()["inv"]["status"], "done")
        self.assertEqual(len(self.ms.docs["loss"]), 1)

    def test_lost_response_no_duplicate(self):
        inv = self.api("post", "", store="sA").get_json()["inv"]
        self.api("put", "/" + inv["id"], lines=[{"id": "p1", "fact": 8}])
        self.ms.fail = ["lost"]                                    # списание дошло до МойСклад, ответ потерялся
        r = self.api("post", f"/{inv['id']}/conduct").get_json()["inv"]
        self.assertEqual(r["loss"]["status"], "queued")
        whops.retry(r["lossOp"])                                   # повтор находит документ по externalCode
        self.assertEqual(len(self.ms.docs["loss"]), 1)
        self.assertEqual(inventory.get(inv["id"])["loss"]["status"], "sent")

    def test_crash_mid_conduct_resumes_same_ops(self):
        inv = self.api("post", "", store="sA").get_json()["inv"]
        self.api("put", "/" + inv["id"], lines=[{"id": "p1", "fact": 8}, {"id": "p2", "fact": 1}])
        orig = whops.create
        n = [0]

        def boom(*a, **k):                                         # оприходование создано, на списании процесс «упал»
            n[0] += 1
            if n[0] == 2:
                raise ValueError("сбой")
            return orig(*a, **k)
        whops.create = boom
        try:
            self.assertEqual(self.api("post", f"/{inv['id']}/conduct").status_code, 400)
        finally:
            whops.create = orig
        self.assertEqual(inventory.get(inv["id"])["status"], "conducting")
        self.ms.book[("p1", "sA")] = 50                            # учёт изменился — снимок не пересчитывается
        r = self.api("post", f"/{inv['id']}/conduct").get_json()["inv"]
        self.assertEqual(r["status"], "done")
        self.assertEqual((len(self.ms.docs["enter"]), len(self.ms.docs["loss"])), (1, 1))
        self.assertEqual(self.ms.docs["loss"][0]["positions"][0]["quantity"], 2)

    def test_store_scoped_and_validation(self):
        self.assertEqual(self.api("post", "", store="nope").status_code, 400)
        inv = self.api("post", "", store="sB").get_json()["inv"]
        self.assertEqual(self.api("put", "/" + inv["id"], lines=[{"id": "zzz", "fact": 1}]).status_code, 400)
        self.assertEqual(self.api("put", "/" + inv["id"], lines=[{"id": "p1", "fact": -1}]).status_code, 400)
        self.api("put", "/" + inv["id"], lines=[{"id": "p1", "fact": 99}])
        self.assertEqual(self.api("post", f"/{inv['id']}/conduct").get_json()["inv"]["lossOp"], "")   # на sB учёт 99 — расхождений нет
        self.assertEqual(self.api("post", "/missing/conduct").status_code, 404)

    def test_delete_draft_and_flag_off(self):
        inv = self.api("post", "", store="sA").get_json()["inv"]
        self.api("delete", "/" + inv["id"])
        self.assertIsNone(inventory.get(inv["id"]))
        self.c.post("/admin/api/warehouse/ops/flag", headers=self.h, json={"on": False})
        self.assertEqual(self.api("post", "", store="sA").status_code, 403)
        self.assertEqual(self.c.get("/admin/api/warehouse/inventory").status_code, 401)


if __name__ == "__main__":
    unittest.main()
