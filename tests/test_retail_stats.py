"""«Сегодня»: выручка розницы из копии МойСклад — заказано (оплаченные, по дате заказа), выкуплено (Доставлен; самовывоз — и Отгружен),
отменено/возвраты, процент выкупа; только розничные заказы; дни — по Алматы (время МойСклад — Москва)."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mirror  # noqa: E402
import order_hook as oh  # noqa: E402
import retail_stats as rs  # noqa: E402

NOW = datetime(2026, 10, 17, 15, 0, tzinfo=timezone(timedelta(hours=5)))    # 15:00 Алматы = 13:00 Москва
ST = {"s-new": "Новый", "s-ok": "Подтвержден", "s-ship": "Отгружен", "s-done": "Доставлен", "s-x": "Отменен", "s-r": "Возврат"}


class RetailStats(unittest.TestCase):
    def setUp(self):
        with mirror.db() as d:
            d.run("DELETE FROM ms_doc")
            for i, (moment, updated, st, total, paid, descr) in enumerate([
                ("2026-10-17 10:00:00", "2026-10-17 10:05:00", "s-ok", 10000, 10000, oh.RETAIL_MARK),     # сегодня, оплачен
                ("2026-10-17 11:00:00", "2026-10-17 11:00:00", "s-new", 5000, 0, oh.RETAIL_MARK),         # не оплачен — не считаем
                ("2026-10-16 23:00:00", "2026-10-16 23:30:00", "s-ok", 7000, 7000, oh.RETAIL_MARK),       # 01:00 Алматы 17-го — сегодня
                ("2026-10-16 21:00:00", "2026-10-16 21:00:00", "s-ok", 3000, 3000, oh.RETAIL_MARK),       # 23:00 Алматы 16-го — вчера
                ("2026-10-10 12:00:00", "2026-10-17 09:00:00", "s-done", 8000, 8000, oh.RETAIL_MARK),     # выкуплен сегодня
                ("2026-10-12 12:00:00", "2026-10-17 09:30:00", "s-ship", 4000, 4000, oh.RETAIL_MARK),     # самовывоз, забрал
                ("2026-10-12 12:00:00", "2026-10-17 09:40:00", "s-ship", 6000, 6000, oh.RETAIL_MARK),     # СДЭК в пути — не выкуп
                ("2026-10-13 12:00:00", "2026-10-17 08:00:00", "s-x", 2000, 0, oh.RETAIL_MARK),           # отменён сегодня
                ("2026-10-17 10:00:00", "2026-10-17 10:00:00", "s-ok", 90000, 90000, "Заказ с сайта"),   # опт — не считаем
            ]):
                d.run("INSERT INTO ms_doc (id, type, number, moment, updated, state_id, sum, paid, descr, deleted) VALUES (%s,'customerorder',%s,%s,%s,%s,%s,%s,%s,0)",
                      (f"o{i}", str(1700 + i), moment, updated, st, total, paid, descr))

    def get(self, period):
        return rs.stats(period, now=NOW, states=ST, pickup=lambda oid: oid == "o5")

    def test_today(self):
        r = self.get("today")
        self.assertEqual((r["ordered"]["count"], r["ordered"]["sum"]), (2, 17000))
        self.assertEqual((r["bought"]["count"], r["bought"]["sum"]), (2, 12000))
        self.assertEqual((r["cancelled"]["count"], r["cancelled"]["sum"]), (1, 2000))
        self.assertEqual(r["buyout"], 67)

    def test_periods(self):
        self.assertEqual(self.get("yesterday")["ordered"], {"count": 1, "sum": 3000})
        self.assertEqual(self.get("7")["ordered"]["count"], 5)                    # с 11.10: ещё два оплаченных от 12.10
        self.assertEqual(rs.bounds("today", NOW), ("2026-10-16 22:00:00", "2026-10-17 13:00:01"))
        with mirror.db() as d:
            d.run("DELETE FROM ms_doc")
        self.assertIsNone(self.get("30")["buyout"])


if __name__ == "__main__":
    unittest.main()
