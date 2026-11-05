"""«Сборка»: новые заказы Ozon/WB → лист (сводка по товарам), подтверждение (Ozon ship одной коробкой, WB — в поставку дня,
нет — создать), ошибки у заказа, этикетки одним PDF в порядке листа, закрытие поставки WB."""
import base64
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_marketplaces as tm  # noqa: E402

import marketplaces as mpk  # noqa: E402
import mp_assembly as asm  # noqa: E402

NOW, iso = tm.NOW, tm.iso


def png():
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (58, 40), "white").save(b, "PNG")
    return b.getvalue()


class Assembly(unittest.TestCase):
    patch, posting = tm.Channels.patch, tm.Channels.posting

    def setUp(self):
        tm.Channels.setUp(self)
        asm._err.clear()
        self.calls = []
        orig_oz, orig_wb = self.oz.__call__, self.wb.__call__
        self.supplies, self.sup_orders, self.ship_fail = [], {}, {}

        def oz(url, json=None, headers=None, timeout=None):
            path = url[len(mpk.CHANNELS["ozon"].api()):]
            if path == "/v4/posting/fbs/ship":
                self.calls.append(("ship", json))
                if json["posting_number"] in self.ship_fail:
                    return tm.Resp(400, {"message": self.ship_fail[json["posting_number"]]})
                return tm.Resp(200, {"result": [json["posting_number"]]})
            if path == "/v2/posting/fbs/package-label":
                self.calls.append(("label", json["posting_number"]))
                from reportlab.pdfgen import canvas
                b = io.BytesIO()
                c = canvas.Canvas(b)
                for _ in json["posting_number"]:
                    c.showPage()
                c.save()
                r = tm.Resp(200, {})
                r.content = b.getvalue()
                return r
            return orig_oz(url, json=json, headers=headers, timeout=timeout)

        def wb(method, url, params=None, json=None, headers=None, timeout=None):
            path = url.split(".wildberries.ru", 1)[1]
            if path == "/api/v3/orders/new":
                return tm.Resp(200, {"orders": [{"id": 5, "createdAt": iso(NOW - 300), "chrtId": 501, "article": "00615",
                                                 "skus": ["8809640738135"], "price": 100000, "currencyCode": 398},
                                                {"id": 6, "createdAt": iso(NOW - 200), "chrtId": 501, "article": "00615",
                                                 "skus": ["8809640738135"], "price": 100000, "currencyCode": 398}]})
            if path == "/api/v3/supplies" and method == "GET":
                return tm.Resp(200, {"supplies": self.supplies, "next": 0})
            if path == "/api/v3/supplies" and method == "POST":
                self.calls.append(("supply", json["name"]))
                self.supplies.append({"id": "WB-GI-1", "name": json["name"], "done": False, "createdAt": iso(NOW)})
                return tm.Resp(201, {"id": "WB-GI-1"})
            if path.startswith("/api/v3/supplies/WB-GI-1/orders/"):
                self.calls.append(("add", path.rsplit("/", 1)[1]))
                self.sup_orders.setdefault("WB-GI-1", []).append(int(path.rsplit("/", 1)[1]))
                return tm.Resp(204, None)
            if path == "/api/v3/supplies/WB-GI-1/orders":
                return tm.Resp(200, {"orders": [{"id": i, "createdAt": iso(NOW), "chrtId": 501, "article": "00615", "skus": [],
                                                 "price": 100000, "currencyCode": 398} for i in self.sup_orders.get("WB-GI-1", [])]})
            if path == "/api/v3/orders/stickers":
                self.calls.append(("stickers", json["orders"]))
                return tm.Resp(200, {"stickers": [{"orderId": i, "file": base64.b64encode(png()).decode()} for i in json["orders"]]})
            if path == "/api/v3/supplies/WB-GI-1/deliver":
                self.calls.append(("deliver",))
                return tm.Resp(204, None)
            if path == "/api/v3/supplies/WB-GI-1/barcode":
                return tm.Resp(200, {"file": base64.b64encode(png()).decode()})
            return orig_wb(method, url, params=params, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "post", oz)
        self.patch(mpk.requests, "request", wb)
        self.patch(mpk, "_me", lambda owner_only=False: ({"role": "owner", "name": "Владелец"}, None))
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(mpk.bp)
        self.c = app.test_client()

    def test_ozon_sheet_ship_labels(self):
        p2 = self.posting(num="0123-0002-1")
        p2["products"][0]["quantity"] = 1
        self.oz.postings = [self.posting(), p2, self.posting(num="0123-0003-1", status="awaiting_deliver")]
        for p in self.oz.postings:
            p["products"][0]["sku"] = 9001
        j = self.c.get("/admin/api/mp/asm?shop=ozon").get_json()
        self.assertEqual([r["ext"] for r in j["orders"]], ["0123-0001-1", "0123-0002-1"])
        self.assertEqual([(x["code"], x["qty"], x["orders"]) for x in j["items"]], [("00615", 3, 2)])   # сводка: всего 3 шт
        self.assertEqual([r["ext"] for r in j["done"]], ["0123-0003-1"])
        self.ship_fail["0123-0002-1"] = "exemplars required"
        r = self.c.post("/admin/api/mp/asm/ship", json={"shop": "ozon", "ids": ["0123-0001-1", "0123-0002-1"]}).get_json()
        self.assertEqual(r["done"], 1)
        self.assertIn("нужна маркировка", r["results"]["0123-0002-1"])
        self.assertEqual(self.calls[0][1]["packages"], [{"products": [{"product_id": 9001, "quantity": 2}]}])
        j = self.c.get("/admin/api/mp/asm?shop=ozon").get_json()
        self.assertIn("маркировка", next(o for o in j["orders"] if o["ext"] == "0123-0002-1")["error"])
        ids = ",".join(f"0123-{i:04d}-1" for i in range(1, 23))                      # 22 → два запроса по 20 и 2, один PDF
        r = self.c.get(f"/admin/api/mp/asm/labels?shop=ozon&ids={ids}")
        self.assertEqual(r.mimetype, "application/pdf")
        from pypdf import PdfReader
        self.assertEqual(len(PdfReader(io.BytesIO(r.data)).pages), 22)
        self.assertEqual([len(c[1]) for c in self.calls if c[0] == "label"], [20, 2])

    def test_wb_supply_created_once_and_stickers(self):
        r = self.c.post("/admin/api/mp/asm/ship", json={"shop": "wb", "ids": ["5", "6"]}).get_json()
        self.assertEqual(r["done"], 2)
        self.assertEqual([c[0] for c in self.calls], ["supply", "add", "add"])           # одна поставка дня на два задания
        self.assertTrue(self.calls[0][1].startswith("AMURA "))
        j = self.c.get("/admin/api/mp/asm?shop=wb").get_json()
        self.assertEqual((j["supply"]["id"], j["supply"]["count"]), ("WB-GI-1", 2))
        r = self.c.get("/admin/api/mp/asm/labels?shop=wb&ids=6,5")
        from pypdf import PdfReader
        pages = PdfReader(io.BytesIO(r.data)).pages
        self.assertEqual(len(pages), 2)
        self.assertAlmostEqual(float(pages[0].mediabox.width) / 72 * 25.4, 58, 0)      # 58×40 мм
        self.assertEqual(self.c.post("/admin/api/mp/asm/close", json={"shop": "wb", "supply": "OTHER"}).status_code, 409)
        self.assertTrue(self.c.post("/admin/api/mp/asm/close", json={"shop": "wb", "supply": "WB-GI-1"}).get_json()["ok"])
        self.assertEqual(self.c.get("/admin/api/mp/asm/qr?shop=wb&supply=WB-GI-1").mimetype, "application/pdf")


    def test_by_orders_split_by_size(self):
        oz, wb = mpk.CHANNELS["ozon"], mpk.CHANNELS["wb"]
        with mpk.db() as d:
            d.run("DELETE FROM mp_order")
        mpk._put(oz, "0123-0001-1", status="awaiting_deliver", number="1903")     # собран — этикетка есть
        mpk._put(oz, "0123-0002-1", status="awaiting_packaging", number="1906")   # ещё не подтверждена сборка
        mpk._put(wb, "777", status="confirm/waiting", number="1910")
        mpk._put(wb, "778", status="new/waiting", number="1911")
        r = asm.by_orders(["1906", "1903", "1910", "1911", "1900"])                # 1900 — заказ сайта
        sh = {g["shop"]: g for g in r["shops"]}
        self.assertEqual(r["own"], ["1900"])
        self.assertEqual((sh["ozon"]["size"], sh["ozon"]["ready"], sh["ozon"]["wait"]),
                         ("75×120", [{"number": "1903", "ext": "0123-0001-1"}], [{"number": "1906", "ext": "0123-0002-1"}]))
        self.assertEqual((sh["wb"]["size"], [x["ext"] for x in sh["wb"]["ready"]], [x["ext"] for x in sh["wb"]["wait"]]),
                         ("58×40", ["777"], ["778"]))
        j = self.c.post("/admin/api/mp/asm/by-orders", json={"numbers": ["1900"]}).get_json()
        self.assertEqual((j["shops"], j["own"]), ([], ["1900"]))                  # только свои — старые наклейки


if __name__ == "__main__":
    unittest.main()
