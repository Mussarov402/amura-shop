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

    def test_off_by_default_and_hidden(self):
        self.assertFalse(modules.enabled("finance"))
        self.assertEqual(self.c.get("/admin/api/modules/on", headers=self.h).get_json()["on"], {})
        self.assertEqual(self.c.get("/admin/api/finance", headers=self.h).status_code, 404)
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
            a, _ = mirror._day_bounds(str(mirror.datetime.now(oh.ALMATY).date()))
            for i, (t, s) in enumerate((("paymentin", 5000), ("cashin", 1500), ("paymentout", 700), ("demand", 9000), ("salesreturn", 1000))):
                d.run("INSERT INTO ms_doc (id, type, moment, sum, applicable, deleted) VALUES (%s, %s, %s, %s, 1, 0)", (f"x{i}", t, a, s))
        j = self.c.get("/admin/api/finance", headers=self.h).get_json()
        self.assertEqual(j["moneyTotal"], 150000)
        self.assertEqual({k: j["flow"][0][k] for k in ("in", "out", "sales", "returns")}, {"in": 6500, "out": 700, "sales": 9000, "returns": 1000})
        self.assertEqual((j["agentsMinus"], j["agents"][0]["name"]), (-4500, "ИП Клиент"))
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "finance", "on": False})
        self.assertEqual(self.c.get("/admin/api/finance", headers=self.h).status_code, 404)


if __name__ == "__main__":
    unittest.main()
