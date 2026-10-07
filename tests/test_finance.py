"""Раздел «Счета»: IBAN, сохранение, журнал, уведомление о смене реквизитов, копия в МойСклад (сеть подменена)."""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "fonts"), exist_ok=True)
for _f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
    if not os.path.exists(os.path.join(ROOT, "fonts", _f)):
        import shutil
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

import inbox  # noqa: E402
import order_hook as oh  # noqa: E402
import finance  # noqa: E402

GOOD = "KZ86125KZT5004100100"   # пример корректного IBAN Казахстана (контрольная сумма сходится)


class FinanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with inbox.db() as d:
            d.run("CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT)")

    def setUp(self):
        self.alerts, self.ms = [], []
        oh.OWNER = "1"
        oh.tg = lambda method, **k: self.alerts.append(k.get("text"))
        oh.ms = lambda m, path, **k: self.ms.append((m, path, k.get("json"))) or {}

    def test_iban(self):
        self.assertTrue(finance.iban_ok(GOOD))
        self.assertTrue(finance.iban_ok("kz86 125K ZT50 0410 0100"))
        self.assertFalse(finance.iban_ok("KZ87125KZT5004100100"))   # не сходится контрольная сумма
        self.assertFalse(finance.iban_ok("KZ8612"))
        self.assertEqual(finance.mask(GOOD), "KZ86 •••• 0100")

    def test_save_edit_delete(self):
        with self.assertRaises(ValueError):
            finance.save_account({"kind": "bank", "name": "Плохой", "iban": "KZ00", "bic": "HSBKKZKX"}, "Владелец")
        aid = finance.save_account({"kind": "bank", "name": "Halyk ИП", "bank": "Halyk", "iban": GOOD, "bic": "hsbkkzkx",
                                    "kbe": "19", "isDefault": True}, "Владелец")
        acc = finance.accounts()
        self.assertEqual(acc[0]["bic"], "HSBKKZKX")
        self.assertIn("добавлен счёт", self.alerts[-1])
        self.assertEqual(self.ms, [])                        # в МойСклад ничего не пишется
        n = len(self.alerts)
        finance.save_account({"id": aid, "kind": "bank", "name": "Halyk ИП (основной)", "iban": GOOD, "bic": "HSBKKZKX"}, "Владелец")
        self.assertEqual(len(self.alerts), n)               # реквизиты не менялись — без тревоги
        finance.save_account({"id": aid, "kind": "bank", "name": "Halyk ИП", "iban": "KZ86 125K ZT50 0410 0100", "bic": "HSBKKZKX"}, "Менеджер")
        kid = finance.save_account({"kind": "kaspi", "name": "Kaspi Gold", "phone": "+7 701 234 56 78"}, "Владелец")
        self.assertEqual(finance.accounts()[-1]["phone"], "+77012345678")
        r = finance.save_routes({"card": str(aid), "kaspi": kid, "cash": "999", "transfer": str(aid)}, "Владелец")
        self.assertEqual(r, {"card": aid, "kaspi": kid, "cash": None})   # чужой id отброшен, опта здесь нет
        finance.delete_account(kid, "Владелец")
        self.assertIsNone(finance.routes()["kaspi"])        # маршрут на удалённый счёт сброшен
        self.assertIn("удалён", self.alerts[-1])
        log = finance.state()["log"]
        self.assertTrue(any("удалён счёт «Kaspi Gold»" in l["text"] for l in log))

    def test_cash_needs_no_iban(self):
        aid = finance.save_account({"kind": "cash", "name": "Касса"}, "Владелец")
        self.assertTrue(any(a["id"] == aid and a["kind"] == "cash" for a in finance.accounts()))


if __name__ == "__main__":
    unittest.main()
