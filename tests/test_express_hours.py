"""Express на сайте только в часы приёма (Пн — нет; Вт–Сб 09:30–17:30; Вс 09:30–15:00), часы настраиваются."""
import os
import sys
import tempfile
import unittest
from datetime import datetime

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import checkout  # noqa: E402
import delivery  # noqa: E402
import order_hook as oh  # noqa: E402


def at(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=oh.ALMATY)     # 2026-10-12 — понедельник


class ExpressHours(unittest.TestCase):
    def test_default_schedule(self):
        st = delivery.express_state
        self.assertEqual(st(at("2026-10-12 12:00")), {"open": False, "next": "завтра в 09:30", "today": ""})   # пн — нет
        self.assertTrue(st(at("2026-10-13 09:30"))["open"])
        self.assertFalse(st(at("2026-10-13 09:29"))["open"])
        self.assertEqual(st(at("2026-10-13 09:00"))["next"], "сегодня в 09:30")
        self.assertFalse(st(at("2026-10-13 17:30"))["open"])
        self.assertTrue(st(at("2026-10-18 14:59"))["open"])                  # вс до 15:00
        self.assertEqual(st(at("2026-10-18 15:00"))["next"], "вт в 09:30")    # после вс — пн выходной
        self.assertEqual(delivery.express_hours_text(), "Вт–Сб 09:30–17:30, Вс 09:30–15:00")

    def test_saved_and_allowed(self):
        delivery.save({"express_hours": [["10:00", "12:00"], None, "x", ["13:00", "11:00"], None, None, None]})
        self.addCleanup(lambda: delivery.save({"express_hours": delivery.EXPRESS_HOURS_DEFAULT}))
        self.assertEqual(delivery.conf()["express_hours"][:4], [["10:00", "12:00"], None, None, None])   # кривое — выключено
        orig = delivery.express_state
        self.addCleanup(lambda: setattr(delivery, "express_state", orig))
        delivery.express_state = lambda now=None: {"open": False, "next": "завтра в 10:00", "today": ""}
        self.assertNotIn("express", checkout.allowed("Алматы"))
        delivery.express_state = lambda now=None: {"open": True, "next": "", "today": "до 12:00"}
        self.assertIn("express", checkout.allowed("Алматы"))
        self.assertEqual(checkout.allowed("Астана"), ("cdek",))

    def test_one_schedule(self):
        """Один график: из него дни курьера «в течение дня» и часы самовывоза на сайте."""
        delivery.save({"express_hours": [None, ["10:00", "18:00"], None, None, None, None, ["11:00", "15:00"]], "store": {"wh_hours": "старый текст"}})
        self.addCleanup(lambda: delivery.save({"express_hours": delivery.EXPRESS_HOURS_DEFAULT}))
        c = delivery.conf()
        self.assertEqual(c["schedule"]["days"], "0100001")
        self.assertEqual(c["store"]["wh_hours"], "Вт 10:00–18:00, Вс 11:00–15:00")
        days = {x["day"] for x in delivery.slots_ahead(now=datetime(2026, 10, 12, 6, 0), days=2)}
        self.assertEqual(days, {"Вт", "Вс"})


if __name__ == "__main__":
    unittest.main()
