"""Адресная этикетка своих заказов (владелец 13.11.2026): поля из заказа МС, 75×120 и 58×40, нет поля — строки нет."""
import io
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ship_label as sl  # noqa: E402

O = {"name": "01234", "sum": 1000000, "payedSum": 1000000, "agent": {"name": "Тестова Айгерим", "phone": "87001234567"},
     "description": "Заказ с розничного сайта\nТестова Айгерим, WhatsApp +77001234567\nГород: Алматы\nОтправка: Пункт выдачи СДЭК — ALM12, ул. Тестовая, 1"}
POS = [{"quantity": 2, "assortment": {"meta": {"type": "product"}}}, {"quantity": 1, "assortment": {"meta": {"type": "service"}}}]


class Label(unittest.TestCase):
    def test_parse(self):
        x = sl.parse(O, POS)
        self.assertEqual((x["name"], x["phone"], x["city"], x["method"], x["addr"]),
                         ("Тестова Айгерим", "+7 700 123 45 67", "Алматы", "Пункт выдачи СДЭК", "ALM12, ул. Тестовая, 1"))
        self.assertEqual((x["lines"], x["qty"], x["paid"], x["track"]), (1, 2, True, ""))      # услуга (доставка) не считается
        self.assertFalse(sl.parse(dict(O, payedSum=0), POS)["paid"])

    def test_pdf_sizes(self):
        from pypdf import PdfReader
        for fmt, (w, h) in sl.SIZES.items():
            pages = PdfReader(io.BytesIO(sl.pdf([sl.parse(O, POS)] * 2, fmt, "+7 700 000 00 00"))).pages
            self.assertEqual(len(pages), 2)                                                   # одна этикетка — одна страница
            self.assertAlmostEqual(float(pages[0].mediabox.width) / 72 * 25.4, w, 0)
            self.assertAlmostEqual(float(pages[0].mediabox.height) / 72 * 25.4, h, 0)
            self.assertIn("01234", pages[0].extract_text())


if __name__ == "__main__":
    unittest.main()
