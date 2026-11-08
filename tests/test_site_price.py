"""Цена розничного сайта «оптовый магазин для всех» (владелец 08.11.2026): правило чистых, упаковка, расходы 6,5 %,
округление вверх до 10, пол — опт + 10; без закупки — старая розница; выключено — ничего не меняется; опт не трогаем."""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inbox  # noqa: E402
import site_price as sp  # noqa: E402

C = dict(sp.DEFAULTS)


class Rule(unittest.TestCase):
    def test_net(self):
        self.assertEqual(sp.net(2000, C), 400)            # до 3 500 — 20 %
        self.assertEqual(sp.net(3500, C), 700)            # от 3 500 — 15 %, не меньше 700
        self.assertEqual(sp.net(6000, C), 900)            # 15 %
        self.assertEqual(sp.net(10000, C), 1000)          # не больше 1 000
        self.assertEqual(sp.net(30000, C), 1500)          # от 20 000 — не меньше 5 %

    def test_price(self):
        # (2000 + 400 + 167) / 0,935 = 2745,45 → 2750
        self.assertEqual(sp.price(2000, 0, C)[0], 2750)
        p, real = sp.price(6000, 0, C)                     # (6000 + 900 + 167) / 0,935 = 7558,3 → 7560
        self.assertEqual(p, 7560)
        self.assertGreaterEqual(real, 900)                  # чистыми не меньше правила (округление — в плюс)
        self.assertEqual(sp.price(2000, 3000, C)[0], 3010)  # пол: опт 3 000 + 10

    def test_apply_and_switch(self):
        with inbox.db() as d:
            d.run("DELETE FROM setting WHERE key='site_price'")
        old = sp.costs
        self.addCleanup(lambda: setattr(sp, "costs", old))
        sp.costs = lambda wait=False: {"a": (2000.0, "2000 KZT")}
        data = lambda: {"items": [{"id": "a", "opt": 2500, "rtl": 3990}, {"id": "b", "opt": 1000, "rtl": 1990},
                                  {"id": "c", "opt": 500, "rtl": 0}], "hiddenItems": [], "outItems": []}
        self.assertEqual([i["rtl"] for i in sp.apply(data())["items"]], [3990, 1990, 0])   # выключено — как было
        sp.save({"on": True})
        got = sp.apply(data())["items"]
        self.assertEqual([i["rtl"] for i in got], [2750, 1990, 0])     # b — без закупки: старая розница; c — нет ни того ни другого
        self.assertEqual((got[0]["rtlMs"], got[0]["opt"]), (3990, 2500))   # розница МС — для таблицы, опт не тронут
        r = {x["id"]: x for x in sp.rows(got, sp.conf(), sp.costs())}
        self.assertEqual((r["a"]["old"], r["a"]["new"], r["b"]["noCost"]), (3990, 2750, True))
        sp.save({"on": False})

    def test_bad_settings(self):
        with self.assertRaises(ValueError):
            sp.save({"fees": 95})


if __name__ == "__main__":
    unittest.main()
