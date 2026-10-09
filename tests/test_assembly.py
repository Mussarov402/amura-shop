"""Лист сборки: товары выбранных заказов и состав каждого заказа (только товары, без услуг)."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402
import admin  # noqa: E402
import order_hook as oh  # noqa: E402

O1 = "1" * 8 + "-1111-1111-1111-" + "1" * 12


def a(t, i, name):
    return {"meta": {"type": t}, "id": i, "name": name, "code": "C" + i}


class Assembly(unittest.TestCase):
    def setUp(self):
        orders = {O1: {"id": O1, "name": "1542", "agent": {"name": "Аружан"},
                       "description": f"{oh.RETAIL_MARK}\nАружан, WhatsApp +7701\nГород: Алматы\nОтправка: Курьер по Алматы (Яндекс) — Абая 10"}}

        def ms(method, path, **kw):
            if path.startswith("/entity/customerorder/"):
                return orders[path.rsplit("/", 1)[1]]
            if path == "/entity/customerorder":
                return {"rows": [{"id": "x2", "name": "1543", "agent": {"name": "Дана"}, "description": "Заказ\nДана\nГород: Астана\nОтправка: СДЭК — ПВЗ"}]}
            raise AssertionError(path)
        pos = {O1: [{"quantity": 2, "assortment": a("product", "p1", "Крем")}, {"quantity": 1, "assortment": a("service", "s1", "Доставка")}],
               "x2": [{"quantity": 1, "assortment": a("variant", "p2", "Тоник (200 мл)")}]}
        self.patch(oh, "ms", ms)
        self.patch(oh, "order_positions", lambda oid: pos[oid])
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Т"})
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def test_assembly(self):
        r = json.loads(self.c.post("/admin/api/orders/assembly", data=json.dumps({"orders": [{"number": "1542", "id": O1}, {"number": "1543"}]}),
                                   content_type="application/json").data)
        self.assertTrue(r["ok"])
        a1, a2 = r["orders"]
        self.assertEqual((a1["number"], a1["client"], a1["ship"]), ("1542", "Аружан", "Курьер по Алматы (Яндекс) — Абая 10"))
        self.assertEqual(a1["lines"], [{"id": "p1", "name": "Крем", "code": "Cp1", "qty": 2}])          # доставка (услуга) — не в сборке
        self.assertEqual((a2["number"], a2["lines"][0]["name"], a2["ship"]), ("1543", "Тоник (200 мл)", "СДЭК — ПВЗ"))

    def test_labels_pdf(self):
        from pypdf import PdfReader
        import io
        items = [{"num": "1542", "name": "Arafat Mussarov", "addr": "Туркебаева 63, кв 12", "kind": "В течение дня", "slot": "14:00–17:30", "phone": "77011234567"},
                 {"num": "1543", "name": "Дана", "addr": "Абая 12", "kind": "EXPRESS"}]
        for fmt, size, pages in (("t58", (58, 40), 2), ("t75", (75, 120), 2), ("a4", (210, 297), 1)):
            r = json.loads(self.c.post("/admin/api/labels", data=json.dumps({"items": items, "fmt": fmt}), content_type="application/json").data)
            pdf = self.c.get(r["url"])
            self.assertEqual(pdf.mimetype, "application/pdf")
            rd = PdfReader(io.BytesIO(pdf.data))
            self.assertEqual(len(rd.pages), pages)
            box = rd.pages[0].mediabox
            self.assertEqual((round(float(box.width) / 72 * 25.4), round(float(box.height) / 72 * 25.4)), size)    # размер страницы = наклейке
            self.assertIn("1542", rd.pages[0].extract_text())
        self.assertEqual(self.c.get("/labels/nope.pdf").status_code, 404)


if __name__ == "__main__":
    unittest.main()
