"""Склад: приёмка и списание из панели — двойная запись в МойСклад без потерь и без дублей (SQLite, подменённый oh.ms)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401  (общая настройка: SQLite, шрифты)
import admin  # noqa: E402
import app  # noqa: E402
import inbox  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402
import requests  # noqa: E402
import whops  # noqa: E402


class FakeMSW:
    """МойСклад для записи: POST создаёт документ; fail — список исключений для очередных POST."""
    def __init__(self):
        self.docs, self.calls, self.fail = {"supply": [], "loss": [], "enter": [], "move": []}, [], []

    def __call__(self, method, path, params=None, json=None, **kw):
        self.calls.append((method, path, params, json))
        ent = path.rsplit("/", 1)[-1]
        if path == "/entity/organization":
            return {"rows": [{"id": "org1"}]}
        if method == "GET" and ent in self.docs:
            code = (params or {}).get("filter", "").split("externalCode=")[-1]
            return {"rows": [d for d in self.docs[ent] if d["externalCode"] == code]}
        if method == "POST" and ent in self.docs:
            doc = {"id": f"{ent}{len(self.docs[ent]) + 1}", "name": f"{len(self.docs[ent]) + 1:05d}", **json}
            if self.fail:
                e = self.fail.pop(0)
                if e == "lost":            # МойСклад принял, но ответ не дошёл
                    self.docs[ent].append(doc)
                    raise requests.ConnectionError("timeout")
                raise e
            self.docs[ent].append(doc)
            return doc
        raise AssertionError(f"неожиданный запрос {method} {path}")


class WhOpsTest(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        oh.organization = lambda: "org1"
        self.ms = oh.ms = FakeMSW()
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + admin.make_admin_token()}
        with mirror.db() as d:
            for t in ("ms_store", "ms_product", "ms_agent"):
                d.run(f"DELETE FROM {t}")
            d.run("DELETE FROM setting WHERE key IN ('mod_warehouse', 'feat_wh_ops')")
            d.run("INSERT INTO ms_store (id, name, archived) VALUES ('sA', 'Основной', 0)")
            d.run("INSERT INTO ms_store (id, name, archived) VALUES ('sB', 'Точка', 0)")
            d.run("INSERT INTO ms_agent (id, name, archived, deleted) VALUES ('k1', 'KorShop', 0, 0)")
            d.run("INSERT INTO ms_product (id, kind, code, name, buy_price, archived, deleted) VALUES ('p1', 'product', '1', 'Крем', 1200, 0, 0)")
            d.run("INSERT INTO ms_product (id, kind, code, name, buy_price, archived, deleted) VALUES ('v1', 'variant', '2', 'Крем 50 мл', 900, 0, 0)")
        with whops.db() as d:
            d.run("DELETE FROM wh_op")

    def on(self):
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "warehouse", "on": True})
        self.c.post("/admin/api/warehouse/ops/flag", headers=self.h, json={"on": True})

    def post(self, **b):
        return self.c.post("/admin/api/warehouse/ops", headers=self.h, json=b)

    def test_switches(self):
        self.assertTrue(self.c.get("/admin/api/warehouse/ops", headers=self.h).get_json()["enabled"])    # решение владельца: включено сразу
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "warehouse", "on": False})
        self.assertEqual(self.c.get("/admin/api/warehouse/ops", headers=self.h).status_code, 404)       # модуль выключен — раздела нет
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "warehouse", "on": True})
        self.c.post("/admin/api/warehouse/ops/flag", headers=self.h, json={"on": False})
        self.assertFalse(self.c.get("/admin/api/warehouse/ops", headers=self.h).get_json()["enabled"])
        self.assertEqual(self.post(kind="loss", store="sA", lines=[{"id": "p1", "qty": 1}]).status_code, 403)
        self.assertEqual(self.ms.calls, [])                                                            # в МойСклад ничего
        self.assertEqual(self.c.post("/admin/api/warehouse/ops/flag", json={"on": True}).status_code, 401)   # без входа нельзя

    def test_supply_sent(self):
        self.on()
        r = self.post(kind="supply", store="sA", agent="k1", descr="накладная 15",
                      lines=[{"id": "p1", "qty": 10, "price": 1100}, {"id": "v1", "qty": 2, "price": 800}]).get_json()
        self.assertEqual((r["op"]["status"], r["op"]["msNumber"]), ("sent", "00001"))
        body = self.ms.docs["supply"][0]
        self.assertEqual(body["externalCode"], r["op"]["id"])
        self.assertEqual(body["agent"]["meta"]["href"].rsplit("/", 1)[-1], "k1")
        self.assertEqual(body["organization"]["meta"]["href"].rsplit("/", 1)[-1], "org1")
        self.assertEqual([(p["assortment"]["meta"]["type"], p["quantity"], p["price"]) for p in body["positions"]],
                         [("product", 10, 110000), ("variant", 2, 80000)])
        self.assertIn("накладная 15", body["description"])
        ops = self.c.get("/admin/api/warehouse/ops", headers=self.h).get_json()["ops"]
        self.assertEqual((ops[0]["storeName"], ops[0]["agentName"], ops[0]["sum"]), ("Основной", "KorShop", 12600))

    def test_loss_uses_buy_price_and_validates(self):
        self.on()
        self.assertEqual(self.post(kind="supply", store="sA", lines=[{"id": "p1", "qty": 1}]).get_json()["error"], "Выберите поставщика")
        self.assertEqual(self.post(kind="loss", store="nope", lines=[{"id": "p1", "qty": 1}]).status_code, 400)
        self.assertEqual(self.post(kind="loss", store="sA", lines=[{"id": "p1", "qty": 0}]).status_code, 400)
        self.assertEqual(self.post(kind="loss", store="sA", lines=[{"id": "zzz", "qty": 1}]).status_code, 400)
        self.assertEqual(self.ms.docs["loss"], [])
        r = self.post(kind="loss", store="sA", descr="брак", lines=[{"id": "p1", "qty": 3, "price": 1}]).get_json()
        self.assertEqual(r["op"]["status"], "sent")
        self.assertEqual(self.ms.docs["loss"][0]["positions"][0]["price"], 120000)      # цена списания — закупочная, не из запроса
        self.assertNotIn("agent", self.ms.docs["loss"][0])

    def test_network_failure_queued_then_retried_once(self):
        self.on()
        self.ms.fail = [requests.ConnectionError("timeout")]
        op = self.post(kind="loss", store="sA", lines=[{"id": "p1", "qty": 1}]).get_json()["op"]
        self.assertEqual((op["status"], op["attempts"]), ("queued", 1))                  # сохранено у нас, не потеряно
        self.assertEqual(self.ms.docs["loss"], [])
        with whops.db() as d:
            d.run("UPDATE wh_op SET next_try=0, created=0")
        self.assertEqual(whops.retry_pending(), 1)
        self.assertEqual((whops.get(op["id"])["status"], len(self.ms.docs["loss"])), ("sent", 1))
        self.assertEqual(whops.retry_pending(), 0)                                        # отправленное больше не трогается

    def test_lost_response_no_duplicate(self):
        self.on()
        self.ms.fail = ["lost"]
        op = self.post(kind="supply", store="sA", agent="k1", lines=[{"id": "p1", "qty": 1, "price": 5}]).get_json()["op"]
        self.assertEqual(op["status"], "queued")
        self.assertEqual(len(self.ms.docs["supply"]), 1)                                  # МойСклад документ принял
        r = self.c.post(f"/admin/api/warehouse/ops/{op['id']}/retry", headers=self.h).get_json()["op"]
        self.assertEqual((r["status"], r["msNumber"]), ("sent", "00001"))
        self.assertEqual(len(self.ms.docs["supply"]), 1)                                  # второго документа нет

    def test_rejected_waits_for_manual_retry(self):
        self.on()
        self.ms.fail = [RuntimeError("МойСклад 412: товар в архиве")]
        op = self.post(kind="loss", store="sA", lines=[{"id": "p1", "qty": 1}]).get_json()["op"]
        self.assertEqual((op["status"], op["error"]), ("error", "МойСклад 412: товар в архиве"))
        with whops.db() as d:
            d.run("UPDATE wh_op SET next_try=0, created=0")
        self.assertEqual(whops.retry_pending(), 0)                                        # сам не повторяет
        r = self.c.post(f"/admin/api/warehouse/ops/{op['id']}/retry", headers=self.h).get_json()["op"]
        self.assertEqual(r["status"], "sent")
        self.assertEqual(len(self.ms.docs["loss"]), 1)

    def test_agents_search(self):
        self.on()
        self.assertEqual([a["name"] for a in self.c.get("/admin/api/warehouse/agents?q=Kor", headers=self.h).get_json()["agents"]], ["KorShop"])


    def test_staff_warehouse_role(self):
        import team
        self.assertEqual(team.PERMS["warehouse"], "Склад")
        self.on()
        old = admin.who
        try:
            admin.who = lambda: {"role": "staff", "perms": ["orders"], "name": "Продавец"}
            self.assertEqual(self.c.get("/admin/api/warehouse/ops").status_code, 403)                # без права «Склад» — нельзя
            self.assertEqual(self.post(kind="loss", store="sA", lines=[{"id": "p1", "qty": 1}]).status_code, 403)
            admin.who = lambda: {"role": "staff", "perms": ["warehouse"], "name": "Кладовщик"}
            self.assertEqual(self.c.get("/admin/api/warehouse").status_code, 200)                     # остатки
            op = self.post(kind="loss", store="sA", lines=[{"id": "p1", "qty": 1}]).get_json()["op"]
            self.assertEqual((op["status"], op["who"]), ("sent", "Кладовщик"))
            self.assertIn("Кладовщик", self.ms.docs["loss"][0]["description"])
            self.assertEqual(self.c.post("/admin/api/warehouse/ops/flag", json={"on": False}).status_code, 403)   # переключатель — только владелец
        finally:
            admin.who = old
        self.assertTrue(whops.enabled())

    def test_enter_and_move(self):
        self.on()
        op = self.post(kind="enter", store="sA", descr="излишки", lines=[{"id": "p1", "qty": 2, "price": 700}]).get_json()["op"]
        self.assertEqual(op["status"], "sent")
        b = self.ms.docs["enter"][0]
        self.assertEqual((b["store"]["meta"]["href"].rsplit("/", 1)[-1], b["positions"][0]["price"]), ("sA", 70000))   # себестоимость — введённая
        self.assertNotIn("agent", b)
        self.assertEqual(self.post(kind="move", store="sA", store2="sA", lines=[{"id": "p1", "qty": 1}]).status_code, 400)   # тот же склад
        self.assertEqual(self.post(kind="move", store="sA", lines=[{"id": "p1", "qty": 1}]).status_code, 400)                # без «куда»
        op = self.post(kind="move", store="sA", store2="sB", lines=[{"id": "p1", "qty": 3, "price": 1}]).get_json()["op"]
        self.assertEqual((op["status"], op["store2"]), ("sent", "sB"))
        m = self.ms.docs["move"][0]
        self.assertEqual((m["sourceStore"]["meta"]["href"].rsplit("/", 1)[-1], m["targetStore"]["meta"]["href"].rsplit("/", 1)[-1]), ("sA", "sB"))
        self.assertNotIn("store", m)
        self.assertEqual(m["positions"][0]["price"], 120000)                                   # перемещение — по закупочной
        ops = self.c.get("/admin/api/warehouse/ops", headers=self.h).get_json()["ops"]
        self.assertEqual((ops[0]["title"], ops[0]["storeName"], ops[0]["store2Name"]), ("Перемещение", "Основной", "Точка"))

    def test_docs_lists(self):
        self.on()
        with mirror.db() as d:
            for t in ("ms_doc", "ms_doc_line"):
                d.run(f"DELETE FROM {t}")
            d.run("INSERT INTO ms_doc (id, type, number, moment, store_id, store2_id, agent_id, sum, applicable, deleted) VALUES"
                  " ('m1', 'move', '00007', '2026-10-10 10:00:00.000', 'sA', 'sB', '', 0, 1, 0)")
            d.run("INSERT INTO ms_doc (id, type, number, moment, store_id, agent_id, sum, applicable, deleted) VALUES"
                  " ('s1', 'supply', '00042', '2026-10-09 09:00:00.000', 'sA', 'k1', 5000, 1, 0)")
            d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, discount, sum) VALUES ('m1', 'x1', 'p1', 4, 0, 0, 0)")
        j = self.c.get("/admin/api/warehouse/docs?type=move", headers=self.h).get_json()
        self.assertEqual(j["total"], 1)
        self.assertEqual({k: j["docs"][0][k] for k in ("number", "store", "store2", "positions", "qty", "at")},
                         {"number": "00007", "store": "Основной", "store2": "Точка", "positions": 1, "qty": 4, "at": "2026-10-10 12:00"})
        self.assertEqual(self.c.get("/admin/api/warehouse/docs?type=supply", headers=self.h).get_json()["docs"][0]["agent"], "KorShop")
        self.assertEqual(self.c.get("/admin/api/warehouse/docs?type=demand", headers=self.h).status_code, 400)
        doc = self.c.get("/admin/api/warehouse/docs/m1", headers=self.h).get_json()["doc"]
        self.assertEqual([(l["name"], l["qty"]) for l in doc["lines"]], [("Крем", 4)])
        self.assertEqual(self.c.get("/admin/api/warehouse/docs/nope", headers=self.h).status_code, 404)

    def test_scanner_lib_served(self):
        r = self.c.get("/admin/vendor/zxing.min.js")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"BrowserMultiFormatReader", r.data)
        self.assertEqual(self.c.get("/admin/vendor/other.js").status_code, 404)

if __name__ == "__main__":
    unittest.main()
