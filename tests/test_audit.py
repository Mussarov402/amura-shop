"""Контроль учёта: приёмки с подозрительными ценами и валютой, минусы с днём появления, поставщики без оплат,
продажи без себестоимости; утреннее сообщение — только при замечаниях и включённом переключателе. МойСклад не трогаем."""
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401
import admin  # noqa: E402
import app  # noqa: E402
import audit  # noqa: E402
import inbox  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402


def ms_t(days=0):
    return (datetime.now(mirror.MS_TZ) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


class AuditTest(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + admin.make_admin_token()}
        self.calls = []
        oh.ms = lambda *a, **k: self.calls.append(a) or (_ for _ in ()).throw(AssertionError("МойСклад не трогаем"))
        with audit.db() as d:
            for t in ("ms_doc", "ms_doc_line", "ms_product", "ms_agent", "ms_agent_balance", "ms_stock", "ms_cost", "audit_neg", "audit_run"):
                d.run(f"DELETE FROM {t}")
            d.run("DELETE FROM setting WHERE key IN ('notif_audit', 'audit_sent_day')")
            for pid, name, buy, rtl in (("p1", "Крем", 0, 6000), ("p2", "Тонер", 3000, 6000), ("p3", "Пенка", 0, 1490)):
                d.run("INSERT INTO ms_product (id, kind, name, buy_price, prices, archived, deleted) VALUES (%s, 'product', %s, %s, %s, 0, 0)",
                      (pid, name, buy, f'{{"Розничная цена": {rtl}, "Оптовая цена": 1}}'))
            for aid, name in (("a1", "Асель Оптом"), ("a2", "KorShop")):
                d.run("INSERT INTO ms_agent (id, name, archived, deleted) VALUES (%s, %s, 0, 0)", (aid, name))
            d.run("INSERT INTO ms_agent_balance (agent_id, name, balance) VALUES ('a1', 'Асель Оптом', 45000000)")
            d.run("INSERT INTO ms_agent_balance (agent_id, name, balance) VALUES ('a2', 'KorShop', 900000)")
            docs = [  # id, type, agent, moment, cur
                ("old1", "supply", "a1", ms_t(20), "USD"), ("old2", "supply", "a1", ms_t(10), "USD"),
                ("new1", "supply", "a1", ms_t(0), ""),                        # Асель обычно в $, а тут ₸
                ("k1", "supply", "a2", ms_t(5), ""), ("pay", "paymentout", "a2", ms_t(3), ""),
                ("sale", "demand", "", ms_t(0), "")]
            for i, t, a, mo, cur in docs:
                d.run("INSERT INTO ms_doc (id, type, number, moment, updated, agent_id, sum, applicable, deleted, cur, rate)"
                      " VALUES (%s, %s, %s, %s, %s, %s, 0, 1, 0, %s, 1)", (i, t, i, mo, mo, a, cur))
            lines = [("old1", "p2", 10, 3000), ("old2", "p2", 10, 3200), ("new1", "p1", 5, 0), ("new1", "p2", 5, 11.92),
                     ("k1", "p2", 1, 3000), ("sale", "p3", 4, 1490), ("sale", "p2", 1, 6000)]
            for n, (doc, pid, q, pr) in enumerate(lines):
                d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, discount, sum) VALUES (%s, %s, %s, %s, %s, 0, %s)",
                      (doc, f"l{n}", pid, q, pr, q * pr))
            d.run("INSERT INTO ms_stock (product_id, store_id, stock, reserve, synced) VALUES ('p3', 'sA', -6, 0, 0)")
            d.run("INSERT INTO ms_stock (product_id, store_id, stock, reserve, synced) VALUES ('p2', 'sA', 5, 0, 0)")

    def test_checks(self):
        r = audit.check()
        sup = {s["number"]: s for s in r["supplies"]}
        self.assertEqual(set(sup), {"new1"})
        s = sup["new1"]
        self.assertIn("обычно в $", s["curFlag"])
        flags = {l["product"]: l["flags"] for l in s["lines"]}
        self.assertEqual(flags["Крем"], ["цена 0"])
        self.assertTrue(any("20% розничной" in f for f in flags["Тонер"]) and any("обычно ~" in f for f in flags["Тонер"]))
        self.assertEqual([(n["product"], n["stock"]) for n in r["negatives"]], [("Пенка", -6)])
        self.assertEqual([x["name"] for x in r["suppliers"]], ["Асель Оптом"])          # KorShop платили — не попадает
        self.assertEqual([(z["product"], z["qty"]) for z in r["zeroCost"]], [("Пенка", 4)])
        self.assertEqual(self.calls, [])                                                # МойСклад не трогали

    def test_negative_since_kept(self):
        audit.check(datetime(2026, 10, 10, 10, tzinfo=oh.ALMATY))
        r = audit.check(datetime(2026, 10, 12, 10, tzinfo=oh.ALMATY))
        self.assertEqual(r["negatives"][0]["since"], "2026-10-10")                       # день, когда минус появился
        with mirror.db() as d:
            d.run("UPDATE ms_stock SET stock=1 WHERE product_id='p3'")
        audit.check()
        with audit.db() as d:
            self.assertEqual(d.run("SELECT COUNT(*) FROM audit_neg", one=True)[0], 0)

    def test_morning_message(self):
        sent = []
        old_tg, old_owner = oh.tg, oh.OWNER
        oh.tg, oh.OWNER = (lambda m, **k: sent.append(k["text"])), "1"
        oh._cache.pop("notif", None)
        try:
            audit.morning()
            self.assertEqual(len(sent), 1)
            self.assertIn("Асель Оптом", sent[0])
            self.assertIn("№new1", sent[0])
            self.assertLessEqual(len(sent[0].split("\n")), 7)                          # коротко: 5–7 строк
            audit.morning()                                                              # раз в день
            self.assertEqual(len(sent), 1)
            with inbox.db() as d:
                inbox.set_setting(d, "notif_audit", "0")
            oh._cache.pop("notif", None)
            audit.morning(force=True)                                                    # выключено — не пишем
            self.assertEqual(len(sent), 1)
        finally:
            oh.tg, oh.OWNER = old_tg, old_owner
            oh._cache.pop("notif", None)

    def test_api_owner_only(self):
        self.assertEqual(self.c.get("/admin/api/audit").status_code, 401)
        j = self.c.get("/admin/api/audit", headers=self.h).get_json()
        self.assertTrue(j["ok"])
        self.assertEqual(j["count"], 4)

    def test_form_hints(self):
        it = {x["id"]: x for x in mirror.warehouse_view(q="")["items"]}
        self.assertEqual((it["p2"]["retail"], it["p2"]["avgBuy"]), (6000, round((30000 + 32000 + 59.6 + 3000) / 26)))
        import whops
        ag = {a["id"]: a["cur"] for a in whops.agents("")}
        self.assertEqual((ag["a1"], ag["a2"]), ("USD", None))   # Асель обычно в долларах (2 приёмки из 3); у KorShop одна приёмка — не судим


if __name__ == "__main__":
    unittest.main()
