"""Деньги: оплаты, выплаты и корректировки из системы — двойная запись в МойСклад без дублей (SQLite, подменённый oh.ms)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401
import admin  # noqa: E402
import app  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402
import payments  # noqa: E402
import requests  # noqa: E402

KINDS = ("paymentin", "paymentout", "cashin", "cashout", "counterpartyadjustment")


class FakePayMS:
    def __init__(self):
        self.docs, self.calls, self.fail = {k: [] for k in KINDS}, [], []
        self.balance = {"k1": -100000.0, "s1": 500000.0}      # ₸; плюс — мы должны

    def __call__(self, method, path, params=None, json=None, **kw):
        self.calls.append((method, path, params, json))
        ent = path.rsplit("/", 1)[-1]
        if path == "/entity/organization":
            return {"rows": [{"id": "org1"}]}
        if path == "/entity/organization/org1/accounts":
            return {"rows": [{"id": "acc1", "bankName": "Kaspi Bank", "accountNumber": "KZ00123456789", "isDefault": True}]}
        if path == "/entity/expenseitem":
            return {"rows": [{"id": "e1", "name": "Закупка товаров"}, {"id": "e2", "name": "Перемещение"}]}
        if path.startswith("/report/counterparty/"):
            return {"balance": self.balance[ent] * 100, "counterparty": {"name": ent}}
        if path == "/report/money/byaccount":
            return {"rows": [{"account": {"meta": {"href": "https://x/accounts/acc1"}, "name": "Kaspi"}, "balance": 100000}]}
        if method == "GET" and ent in self.docs:
            code = (params or {}).get("filter", "").split("externalCode=")[-1]
            return {"rows": [d for d in self.docs[ent] if d["externalCode"] == code]}
        if method == "POST" and ent in self.docs:
            doc = {"id": f"{ent}{len(self.docs[ent]) + 1}", "name": f"{len(self.docs[ent]) + 1:05d}", **json}
            if self.fail:
                e = self.fail.pop(0)
                if e == "lost":
                    self._apply(ent, json)
                    self.docs[ent].append(doc)
                    raise requests.ConnectionError("timeout")
                raise e
            self._apply(ent, json)
            self.docs[ent].append(doc)
            return doc
        raise AssertionError(f"неожиданный запрос {method} {path}")

    def _apply(self, ent, j):
        aid = j["agent"]["meta"]["href"].rsplit("/", 1)[-1]
        s = j["sum"] / 100
        self.balance[aid] += s if ent in ("paymentin", "cashin", "counterpartyadjustment") else -s


class PaymentsTest(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        oh.organization = lambda: "org1"
        self.ms = oh.ms = FakePayMS()
        payments._meta.clear()
        payments._bg = lambda fn, *a: fn(*a)      # обновление баланса — синхронно, чтобы тест видел результат
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + admin.make_admin_token()}
        with mirror.db() as d:
            for t in ("ms_agent", "ms_agent_balance", "ms_doc", "ms_money"):
                d.run(f"DELETE FROM {t}")
            d.run("DELETE FROM setting WHERE key IN ('mod_finance', 'feat_pay_ops')")
            d.run("INSERT INTO ms_agent (id, name, phone, archived, deleted) VALUES ('k1', 'Ertos', '+77010000001', 0, 0)")
            d.run("INSERT INTO ms_agent (id, name, phone, archived, deleted) VALUES ('s1', 'Ludmila', '', 0, 0)")
            d.run("INSERT INTO ms_agent_balance (agent_id, name, balance, synced) VALUES ('k1', 'Ertos', -100000, 0)")
            d.run("INSERT INTO ms_agent_balance (agent_id, name, balance, synced) VALUES ('s1', 'Ludmila', 500000, 0)")
            d.run("INSERT INTO ms_doc (id, type, number, moment, agent_id, sum, applicable, deleted) VALUES ('dm1', 'demand', '7', '2026-10-10 10:00:00', 'k1', 100000, 1, 0)")
        with payments.db() as d:
            d.run("DELETE FROM pay_op")

    def post(self, **b):
        return self.c.post("/admin/api/money/ops", headers=self.h, json=b)

    def bal(self, aid):
        with mirror.db() as d:
            return d.run("SELECT balance FROM ms_agent_balance WHERE agent_id=%s", (aid,), one=True)[0]

    def test_payment_in_bank_and_balance_refresh(self):
        r = self.post(dir="in", agent="k1", sum=60000, account="acc1", descr="за отгрузку 7").get_json()
        self.assertEqual((r["op"]["kind"], r["op"]["status"]), ("paymentin", "sent"))
        doc = self.ms.docs["paymentin"][0]
        self.assertEqual(doc["sum"], 6000000)
        self.assertEqual(doc["externalCode"], r["op"]["id"])
        self.assertTrue(doc["organizationAccount"]["meta"]["href"].endswith("/entity/organization/org1/accounts/acc1"))
        self.assertEqual(doc["paymentPurpose"], "за отгрузку 7")
        self.assertNotIn("expenseItem", doc)
        self.assertEqual(self.bal("k1"), -40000)                 # баланс перечитан из МойСклад сразу

    def test_payout_cash_needs_expense(self):
        self.assertEqual(self.post(dir="out", agent="s1", sum=600000, account="cash").status_code, 400)
        r = self.post(dir="out", agent="s1", sum=600000, account="cash", expense="e2", day="2026-10-01").get_json()["op"]
        self.assertEqual(r["kind"], "cashout")
        doc = self.ms.docs["cashout"][0]
        self.assertTrue(doc["expenseItem"]["meta"]["href"].endswith("/entity/expenseitem/e2"))
        self.assertNotIn("organizationAccount", doc)
        self.assertEqual(doc["moment"], "2026-10-01 10:00:00")    # полдень Алматы = 10:00 Москвы
        self.assertEqual(self.bal("s1"), -100000)

    def test_adjustment_signed(self):
        r = self.post(dir="adj", agent="s1", sum=-25645.7, descr="курс").get_json()["op"]
        self.assertEqual(r["kind"], "counterpartyadjustment")
        doc = self.ms.docs["counterpartyadjustment"][0]
        self.assertEqual(doc["sum"], -2564570)
        self.assertNotIn("paymentPurpose", doc)
        self.assertEqual(self.bal("s1"), 500000 - 25645.7)       # минус уменьшает «мы должны»
        self.assertEqual(self.post(dir="adj", agent="s1", sum=0).status_code, 400)

    def test_lost_response_no_duplicate(self):
        self.ms.fail = ["lost"]
        r = self.post(dir="in", agent="k1", sum=1000, account="acc1").get_json()["op"]
        self.assertEqual(r["status"], "queued")
        payments.retry(r["id"])
        self.assertEqual(len(self.ms.docs["paymentin"]), 1)
        self.assertEqual(payments.get(r["id"])["status"], "sent")

    def test_rejected_then_retry(self):
        self.ms.fail = [RuntimeError("МойСклад 412: неверная статья")]
        r = self.post(dir="in", agent="k1", sum=1000, account="acc1").get_json()["op"]
        self.assertEqual(r["status"], "error")
        self.assertEqual(payments.retry_pending(), 0)            # отказ сам не повторяется
        r = self.c.post(f"/admin/api/money/ops/{r['id']}/retry", headers=self.h).get_json()["op"]
        self.assertEqual(r["status"], "sent")

    def test_validation(self):
        for b in ({"dir": "in", "agent": "nope", "sum": 1, "account": "acc1"}, {"dir": "in", "agent": "k1", "sum": -5, "account": "acc1"},
                  {"dir": "in", "agent": "k1", "sum": 5, "account": "zzz"}, {"dir": "in", "agent": "k1", "sum": 5, "account": "acc1", "day": "2099-01-01"},
                  {"dir": "in", "agent": "k1", "sum": 1e12, "account": "acc1"}, {"dir": "x", "agent": "k1", "sum": 5}):
            self.assertEqual(self.post(**b).status_code, 400, b)
        self.assertEqual(sum(len(v) for v in self.ms.docs.values()), 0)

    def test_owner_only_and_switches(self):
        self.assertEqual(self.c.post("/admin/api/money/ops", json={"dir": "in"}).status_code, 401)
        self.assertEqual(self.c.get("/admin/api/money/debts").status_code, 401)
        self.c.post("/admin/api/money/ops/flag", headers=self.h, json={"on": False})
        self.assertEqual(self.post(dir="in", agent="k1", sum=5, account="acc1").status_code, 403)
        self.assertFalse(self.c.get("/admin/api/money/ops", headers=self.h).get_json()["enabled"])
        self.c.post("/admin/api/money/ops/flag", headers=self.h, json={"on": True})
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "finance", "on": False})
        self.assertEqual(self.c.get("/admin/api/money/ops", headers=self.h).status_code, 404)

    def test_card_debts_meta(self):
        self.post(dir="in", agent="k1", sum=1000, account="acc1")
        c = self.c.get("/admin/api/money/agents/k1", headers=self.h).get_json()["agent"]
        self.assertEqual((c["name"], c["balance"]), ("Ertos", -99000))
        self.assertEqual([x["title"] for x in c["docs"]], ["Отгрузка"])
        self.assertEqual(c["ops"][0]["kind"], "paymentin")
        d = self.c.get("/admin/api/money/debts?side=us", headers=self.h).get_json()
        self.assertEqual((d["count"], d["total"], d["rows"][0]["name"]), (1, 99000, "Ertos"))
        d = self.c.get("/admin/api/money/debts?side=we&q=Lud", headers=self.h).get_json()
        self.assertEqual((d["count"], d["total"]), (1, 500000))
        m = self.c.get("/admin/api/money/meta", headers=self.h).get_json()
        self.assertEqual([a["id"] for a in m["accounts"]], ["acc1", "cash"])
        self.assertEqual(self.c.get("/admin/api/money/agents?q=Ert", headers=self.h).get_json()["agents"][0]["balance"], -99000)
        self.assertEqual(self.c.get("/admin/api/money/agents/zzz", headers=self.h).status_code, 404)


if __name__ == "__main__":
    unittest.main()
