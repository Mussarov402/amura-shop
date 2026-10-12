"""Этикетки и ценники со штрихкодом: PDF по карточкам из нашей копии, подписанная ссылка, доступ."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401
import admin  # noqa: E402
import app  # noqa: E402
import labels  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402
from pypdf import PdfReader  # noqa: E402
import io  # noqa: E402


class LabelsTest(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + admin.make_admin_token()}
        with mirror.db() as d:
            d.run("DELETE FROM ms_product")
            d.run("INSERT INTO ms_product (id, kind, code, name, prices, barcodes, archived, deleted) VALUES ('p1', 'product', '00012', 'Крем', %s, %s, 0, 0)",
                  (json.dumps({"Оптовая цена": 2500, "Розничная цена": 3990}), json.dumps(["4601234567893"])))
            d.run("INSERT INTO ms_product (id, kind, code, name, prices, barcodes, archived, deleted) VALUES ('p2', 'product', '00013', 'Маска', '{}', '[]', 0, 0)")

    def pages(self, b):
        return len(PdfReader(io.BytesIO(b)).pages)

    def test_pdf(self):
        self.assertTrue(labels._ean_ok("4601234567893"))
        self.assertFalse(labels._ean_ok("4601234567890"))
        self.assertEqual(self.pages(labels.pdf([{"id": "p1", "qty": 3}, {"id": "p2"}])), 4)          # термо: по странице на этикетку
        self.assertEqual(self.pages(labels.pdf([{"id": "p1", "qty": 30}], "price", "a4")), 2)        # A4: 24 на листе
        self.assertEqual(labels.products(["p1"])["p1"]["price"], 3990)                                 # по умолчанию розничная
        self.assertEqual(labels.products(["p1"], "Оптовая цена")["p1"]["price"], 2500)
        self.assertEqual(labels.price_types(["p1", "p2"]), ["Розничная цена", "Оптовая цена"])
        with self.assertRaises(ValueError):
            labels.pdf([{"id": "zzz"}])
        with self.assertRaises(ValueError):
            labels.pdf([{"id": "p1", "qty": 5000}])

    def test_signed_link(self):
        r = self.c.post("/admin/api/product-labels", headers=self.h, json={"items": [{"id": "p1", "qty": 2}], "kind": "price"}).get_json()
        url = r["url"]
        f = self.c.get(url)
        self.assertEqual((f.status_code, f.mimetype), (200, "application/pdf"))
        self.assertEqual(self.pages(f.data), 2)
        self.assertEqual(self.c.get(url.split("?")[0] + "?t=bad").status_code, 404)                   # без подписи — нет
        self.assertEqual(self.c.post("/admin/api/product-labels", json={"items": [{"id": "p1"}]}).status_code, 401)
        self.assertEqual(self.c.post("/admin/api/product-labels", headers=self.h, json={"items": []}).status_code, 400)
        self.assertEqual(self.c.get("/admin/api/product-labels/meta?ids=p1", headers=self.h).get_json()["priceTypes"][0], "Розничная цена")


if __name__ == "__main__":
    unittest.main()
