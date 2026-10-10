"""Подключаемые модули: по умолчанию выключены, выключенный — невидим (API 404); модуль «Финансы» читает зеркало."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402  (общая настройка: SQLite, шрифты, подменённый oh.ms)
import admin  # noqa: E402
import app  # noqa: E402
import inbox  # noqa: E402
import mirror  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402


class ModulesTest(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        oh.ms = tm.FakeMS()
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + admin.make_admin_token()}
        with inbox.db() as d:
            d.run("DELETE FROM setting WHERE key LIKE %s", ("mod_%",))

    def test_on_by_default_and_switch_off_hides(self):
        # решение владельца 10.10.2026: готовые разделы включены сразу, выключатель — для отката
        self.assertTrue(modules.enabled("finance"))
        self.assertEqual(self.c.get("/admin/api/modules/on", headers=self.h).get_json()["on"], {"finance": True, "warehouse": True, "cards": True})
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "finance", "on": False})
        self.assertFalse(modules.enabled("finance"))                               # выключили вручную — так и остаётся
        self.assertEqual(self.c.get("/admin/api/finance", headers=self.h).status_code, 404)
        self.assertEqual(self.c.get("/admin/api/modules/on", headers=self.h).get_json()["on"], {"warehouse": True, "cards": True})
        self.assertEqual(self.c.get("/admin/api/modules").status_code, 401)        # без входа — нельзя

    def test_toggle_and_view(self):
        r = self.c.put("/admin/api/modules", headers=self.h, json={"id": "finance", "on": True}).get_json()
        self.assertTrue(next(m for m in r["modules"] if m["id"] == "finance")["on"])
        self.assertEqual(self.c.put("/admin/api/modules", headers=self.h, json={"id": "nope", "on": True}).status_code, 404)
        with mirror.db() as d:
            for t in ("ms_money", "ms_agent_balance", "ms_doc"):
                d.run(f"DELETE FROM {t}")
            d.run("INSERT INTO ms_money (account_id, name, balance, synced) VALUES ('a1', 'Kaspi', 120000, 1)")
            d.run("INSERT INTO ms_money (account_id, name, balance, synced) VALUES ('cash:o', 'Касса', 30000, 1)")
            d.run("INSERT INTO ms_agent_balance (agent_id, name, balance, synced) VALUES ('c1', 'ИП Клиент', -4500, 1)")
            d.run("INSERT INTO ms_agent_balance (agent_id, name, balance, synced) VALUES ('s1', 'KorShop', 800000, 1)")
            a, _ = mirror._day_bounds(str(mirror.datetime.now(oh.ALMATY).date()))
            for i, (t, s) in enumerate((("paymentin", 5000), ("cashin", 1500), ("paymentout", 700), ("demand", 9000), ("salesreturn", 1000))):
                d.run("INSERT INTO ms_doc (id, type, moment, sum, applicable, deleted) VALUES (%s, %s, %s, %s, 1, 0)", (f"x{i}", t, a, s))
        j = self.c.get("/admin/api/finance", headers=self.h).get_json()
        self.assertEqual(j["moneyTotal"], 150000)
        self.assertEqual({k: j["flow"][0][k] for k in ("in", "out", "sales", "returns")}, {"in": 6500, "out": 700, "sales": 9000, "returns": 1000})
        self.assertEqual((j["owedUsTotal"], j["owedUs"][0]["name"], j["owedUs"][0]["sum"]), (4500, "ИП Клиент", 4500))   # минус — должен нам
        self.assertEqual((j["weOweTotal"], j["weOwe"][0]["name"]), (800000, "KorShop"))                         # плюс — мы должны
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "finance", "on": False})
        self.assertEqual(self.c.get("/admin/api/finance", headers=self.h).status_code, 404)

    def test_unpaid(self):
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "finance", "on": True})
        with mirror.db() as d:
            for t in ("ms_doc", "ms_payment_link", "ms_agent"):
                d.run(f"DELETE FROM {t}")
            d.run("INSERT INTO ms_agent (id, name, deleted) VALUES ('c1', 'ИП Клиент', 0)")
            docs = (("u1", "demand", 9000, 0, 1),     # не оплачена
                    ("u2", "demand", 5000, 5000, 1),  # оплачена по payedSum
                    ("u3", "demand", 6000, 0, 1),     # оплачена связью платежа (payedSum ещё не обновился)
                    ("u4", "demand", 8000, 3000, 1),  # частично
                    ("u5", "demand", 7000, 0, 0),     # не проведена — не считаем
                    ("s1", "supply", 50000, None, 1))  # приёмка до первого полного прохода (paid пусто)
            for i, (did, t, s, paid, ap) in enumerate(docs):
                d.run("INSERT INTO ms_doc (id, type, number, moment, agent_id, sum, paid, applicable, deleted) VALUES (%s, %s, %s, %s, 'c1', %s, %s, %s, 0)",
                      (did, t, did, f"2026-10-0{i + 1} 10:00:00.000", s, paid, ap))
            d.run("INSERT INTO ms_payment_link (payment_id, doc_id, doc_type, sum) VALUES ('p1', 'u3', 'demand', 6000)")
        u = self.c.get("/admin/api/finance", headers=self.h).get_json()["unpaid"]
        self.assertEqual((u["demand"]["total"], u["demand"]["count"]), (14000, 2))
        self.assertEqual([(r["number"], r["due"], r["paid"], r["agent"]) for r in u["demand"]["rows"]],
                         [("u4", 5000, 3000, "ИП Клиент"), ("u1", 9000, 0, "ИП Клиент")])   # свежие сверху
        self.assertEqual((u["supply"]["total"], u["supply"]["rows"][0]["day"]), (50000, "2026-10-06"))
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "finance", "on": False})
        self.assertEqual(self.c.get("/admin/api/finance", headers=self.h).status_code, 404)


    def test_warehouse(self):
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "warehouse", "on": False})
        self.assertEqual(self.c.get("/admin/api/warehouse", headers=self.h).status_code, 404)   # выключен — не виден
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "warehouse", "on": True})
        self.assertTrue(self.c.get("/admin/api/modules/on", headers=self.h).get_json()["on"]["warehouse"])
        with mirror.db() as d:
            for t in ("ms_store", "ms_stock", "ms_product"):
                d.run(f"DELETE FROM {t}")
            for sid, name, arch in (("sA", "Основной", 0), ("sB", "Магазин", 0), ("sZ", "Старый", 1)):
                d.run("INSERT INTO ms_store (id, name, archived) VALUES (%s, %s, %s)", (sid, name, arch))
            for pid, name, code, bp, arch in (("p1", "Крем дневной", "101", 1000, 0), ("p2", "Крем ночной", "102", 2000, 0),
                                              ("p3", "Сыворотка", "103", 0, 0), ("p4", "Крем старый", "104", 0, 1)):
                d.run("INSERT INTO ms_product (id, kind, code, name, buy_price, archived, deleted, barcodes) VALUES (%s, 'product', %s, %s, %s, %s, 0, %s)",
                      (pid, code, name, bp, arch, f'["460{code}"]'))
            for pid, sid, st, rs in (("p1", "sA", 5, 1), ("p1", "sB", 2, 0), ("p2", "sA", 3, 0), ("p3", "sB", -1, 0)):
                d.run("INSERT INTO ms_stock (product_id, store_id, stock, reserve, synced) VALUES (%s, %s, %s, %s, 1)", (pid, sid, st, rs))
        g = lambda qs="": self.c.get("/admin/api/warehouse" + qs, headers=self.h).get_json()
        j = g()
        self.assertEqual([(s["name"], s["units"], s["skus"], s["value"], s["reserve"]) for s in j["stores"]],
                         [("Магазин", 2, 1, 2000, 0), ("Основной", 8, 2, 11000, 1)])          # архивный склад скрыт
        self.assertEqual([(x["name"], x["stock"]) for x in j["items"]], [("Крем дневной", 7), ("Крем ночной", 3), ("Сыворотка", -1)])
        self.assertEqual([(s["store"], s["stock"], s["reserve"]) for s in j["items"][0]["stores"]], [("Магазин", 2, 0), ("Основной", 5, 1)])
        self.assertEqual([x["name"] for x in g("?q=Крем")["items"]], ["Крем дневной", "Крем ночной"])   # архивный товар не виден
        self.assertEqual([x["name"] for x in g("?q=460103")["items"]], ["Сыворотка"])                   # по штрихкоду
        self.assertEqual([x["name"] for x in g("?mode=out")["items"]], ["Сыворотка"])
        self.assertEqual([(x["name"], x["stock"]) for x in g("?store=sB&mode=in")["items"]], [("Крем дневной", 2)])
        self.assertEqual(g("?mode=in")["total"], 2)
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "warehouse", "on": False})
        self.assertEqual(self.c.get("/admin/api/warehouse", headers=self.h).status_code, 404)

if __name__ == "__main__":
    unittest.main()
