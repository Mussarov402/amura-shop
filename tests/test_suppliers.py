"""Поставщики на опте (05.11.2026): цена по формуле, источники (таблица, файл, МойСклад поставщика), шифрование
токена и маска, сопоставление по штрихкоду, «На проверке» → «Добавить» (карточка в МойСклад только по кнопке)."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask  # noqa: E402

import admin  # noqa: E402
import inbox  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402
import suppliers as sp  # noqa: E402

TOKEN = "ms-token-SECRET-1234567890abcd"


class Resp:
    def __init__(self, code, body=None, content=b""):
        self.status_code, self._b, self.content = code, body, content

    def json(self):
        return self._b


class Suppliers(unittest.TestCase):
    def setUp(self):
        self.patch(oh, "ORDER_SECRET", b"test-secret")         # на сервере — байты, как в order_hook
        with sp.db() as d:
            for t in ("supplier", "sup_item", "sup_review"):
                d.run(f"DELETE FROM {t}")
            d.run("DELETE FROM setting WHERE key IN ('sup_fees','sup_note')")
        modules.update("suppliers", on=True)
        self.addCleanup(lambda: modules.update("suppliers", on=False))
        self.role = "owner"
        self.patch(admin, "who", lambda: {"role": self.role, "perms": ["orders", "products"], "name": "В"})
        self.patch(oh, "live", lambda *a, **k: {"items": [{"id": "p1", "name": "Anua тонер", "barcode": "8809640730000", "qty": 0}]})
        self.ms = []
        self.patch(oh, "ms", lambda method, path, json=None, **k: self.ms.append((method, path, json)) or {"id": "new-prod"})
        app = Flask(__name__)
        app.register_blueprint(sp.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def test_price_formula(self):
        f = {"acq": 3.5, "tax": 2.0, "cash": 1.0}
        self.assertEqual(sp.sale_price(4000, 150, 0, f), 4440)              # 4150 / 0.935 = 4438.5 → 4440
        self.assertEqual(sp.sale_price(4000, 150, 100, f), 4340)            # скидка поставщика 100 ₸: 4050 / 0.935 = 4331.6 → 4340
        self.assertEqual(sp.sale_price(0, 150, 0, f), 0)
        self.assertEqual(sp.sale_price(1000, 0, 0, {"a": 0}), 1000)        # без расходов и наценки — ровно

    def test_parse_and_sheet_url(self):
        rows = [["Штрих-код", "Наименование", "Остаток", "Цена"], ["8809640730000.0", "Anua", "5", "4 000,50"], ["", "без шк", "1", "1"],
                ["123", "короткий", "1", "1"]]
        self.assertEqual(sp.parse_rows(rows), [{"barcode": "8809640730000", "name": "Anua", "stock": 5.0, "price": 4000.5}])
        with self.assertRaises(ValueError):
            sp.parse_rows([["Название", "Остаток"], ["x", "1"]])
        self.assertEqual(sp.sheet_url("https://docs.google.com/spreadsheets/d/AbC-1/edit#gid=42"),
                         "https://docs.google.com/spreadsheets/d/AbC-1/export?format=csv&gid=42")

    def test_token_sealed_masked_never_returned(self):
        r = self.c.post("/admin/api/suppliers", json={"name": "Сосед", "src": "ms", "token": TOKEN, "markup": 150})
        self.assertEqual(r.status_code, 200, r.get_json())
        with sp.db() as d:
            raw = d.run("SELECT secret FROM supplier", one=True)[0]
        self.assertNotIn(TOKEN, raw)
        self.assertEqual(sp.unseal(raw), TOKEN)
        j = self.c.get("/admin/api/suppliers").get_json()
        self.assertNotIn(TOKEN, json.dumps(j, ensure_ascii=False))
        self.assertEqual(j["suppliers"][0]["token"], "••••abcd")
        sid = r.get_json()["id"]
        self.c.put(f"/admin/api/suppliers/{sid}", json={"name": "Сосед 2"})            # без токена — прежний остаётся
        self.assertEqual(sp.unseal(sp.get(sid)["secret"]), TOKEN)

    def test_owner_only_and_module_off(self):
        self.role = "staff"
        self.assertEqual(self.c.get("/admin/api/suppliers").status_code, 403)
        self.role = "owner"
        modules.update("suppliers", on=False)
        self.assertEqual(self.c.get("/admin/api/suppliers").status_code, 404)

    def test_sheet_sync_match_and_review(self):
        csv_text = "Штрихкод;Название;Остаток;Цена\n8809640730000;Anua тонер;3;4000\n8801234567893;Новый крем;2;2500\n"
        sid = self.c.post("/admin/api/suppliers", json={"name": "Сосед", "src": "sheet",
                                                         "url": "https://docs.google.com/spreadsheets/d/X/edit"}).get_json()["id"]
        with mock.patch.object(sp.requests, "get", lambda url, timeout=None: Resp(200, content=csv_text.encode())) as _:
            self.assertEqual(self.c.post(f"/admin/api/suppliers/{sid}/sync").get_json()["items"], 2)
        j = self.c.get("/admin/api/suppliers").get_json()
        self.assertEqual([m["ourId"] for m in j["matched"]], ["p1"])
        self.assertEqual(j["matched"][0]["price"], 4440)
        self.assertEqual([x["barcode"] for x in j["review"]], ["8801234567893"])
        self.assertEqual(self.ms, [])                                                   # без кнопки МойСклад не трогаем
        r = self.c.post("/admin/api/suppliers/review", json={"sup": sid, "barcode": "8801234567893", "action": "add"})
        self.assertEqual(r.get_json()["productId"], "new-prod")
        self.assertEqual(self.ms[0][:2], ("POST", "/entity/product"))
        self.assertEqual(self.ms[0][2]["barcodes"], [{"ean13": "8801234567893"}])
        self.assertEqual(self.c.get("/admin/api/suppliers").get_json()["review"][0]["state"], "added")

    def test_hide_and_file_upload(self):
        sid = self.c.post("/admin/api/suppliers", json={"name": "Без учёта", "src": "file"}).get_json()["id"]
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Штрихкод", "Название", "Остаток", "Цена"])
        ws.append([8801234567893, "Крем", 4, 2500])
        buf = io.BytesIO()
        wb.save(buf)
        r = self.c.post(f"/admin/api/suppliers/{sid}/sync", data={"file": (io.BytesIO(buf.getvalue()), "остатки.xlsx")},
                        content_type="multipart/form-data")
        self.assertEqual(r.get_json()["items"], 1)
        self.c.post("/admin/api/suppliers/review", json={"sup": sid, "barcode": "8801234567893", "action": "hide"})
        self.assertEqual(self.c.get("/admin/api/suppliers").get_json()["review"], [])

    def test_ms_adapter_and_error_without_token(self):
        seen = {}

        def get(url, headers=None, params=None, timeout=None):
            seen.update(auth=headers["Authorization"])
            return Resp(200, {"rows": [{"name": "Anua", "barcodes": [{"ean13": "8809640730000"}], "stock": 7,
                                        "salePrices": [{"value": 400000, "priceType": {"name": "Розничная"}},
                                                       {"value": 350000, "priceType": {"name": "Оптовая"}}]},
                                       {"name": "без шк", "barcodes": [], "stock": 1, "salePrices": []}]})
        with mock.patch.object(sp.requests, "get", get):
            self.assertEqual(sp.from_ms(TOKEN, "Оптовая"), [{"barcode": "8809640730000", "name": "Anua", "stock": 7.0, "price": 3500.0}])
        self.assertEqual(seen["auth"], "Bearer " + TOKEN)
        sid = self.c.post("/admin/api/suppliers", json={"name": "МС", "src": "ms", "token": TOKEN}).get_json()["id"]
        out = io.StringIO()
        with mock.patch.object(sp.requests, "get", lambda *a, **k: Resp(401, {})), mock.patch("sys.stdout", out):
            r = self.c.post(f"/admin/api/suppliers/{sid}/sync")
        self.assertEqual(r.status_code, 502)
        self.assertIn("токен", r.get_json()["error"])
        self.assertNotIn(TOKEN, out.getvalue() + json.dumps(r.get_json()))              # токен не в журнале и не в ответе

    def test_fees_settings(self):
        r = self.c.put("/admin/api/suppliers/settings", json={"fees": {"acq": 3, "tax": 2, "cash": 0}, "note": "под заказ, 1 день"}).get_json()
        self.assertEqual((r["fees"], r["note"]), ({"acq": 3.0, "tax": 2.0, "cash": 0.0}, "под заказ, 1 день"))
        self.assertEqual(self.c.put("/admin/api/suppliers/settings", json={"fees": {"acq": 40}}).status_code, 400)



class OptStorefront(unittest.TestCase):
    """Витрина опта и заказ: остаток у партнёра, «под заказ», цена по формуле, только курьер по Алматы, пометка в заказе."""
    patch = Suppliers.patch

    def setUp(self):
        Suppliers.setUp(self)
        sid = self.c.post("/admin/api/suppliers", json={"name": "Сосед", "src": "file", "markup": 150}).get_json()["id"]
        sp.store(sid, [{"barcode": "8809640730000", "name": "Anua", "stock": 4, "price": 4000},
                       {"barcode": "8801234567893", "name": "Новый крем", "stock": 2, "price": 2500}])
        self.data = {"updated": "x", "items": [{"id": "own1", "name": "Свой", "barcode": "111111111111", "qty": 5, "opt": 3000, "rtl": 4000}],
                     "outItems": [{"id": "p1", "name": "Anua тонер", "barcode": "8809640730000", "qty": 0, "opt": 0, "rtl": 6000}]}
        sp._offers["t"] = sp._aug["t"] = 0.0
        self.tg, self.posted = [], []
        for name, val in {"live": lambda *a, **k: self.data, "too_many": lambda *a, **k: False, "find_or_create_agent": lambda *a: "agent-1",
                          "organization": lambda: "org-1", "store_id": lambda: "store-1", "fee_service_id": lambda: "fee-1",
                          "delivery_service_id": lambda: "dlv-1", "notify_bg": lambda fn: None, "is_wholesale": lambda cid: True,
                          "opt_min": lambda: 0, "tg": lambda method, **k: self.tg.append(k.get("text", "")) or {},
                          "_post_order": lambda body: self.posted.append(body) or {"name": "1907", "id": "o1"}}.items():
            self.patch(oh, name, val)
        self.patch(oh, "catalog", lambda: {i["id"]: i for i in self.data["items"]})

    def test_off_by_default_then_augment(self):
        self.assertFalse(sp.live_on())
        self.c.put("/admin/api/suppliers/settings", json={"live": True})
        self.assertTrue(sp.live_on())
        v = {i["id"]: i for i in sp.augment(self.data)["items"]}
        self.assertEqual((v["p1"]["qty"], v["p1"]["own"], v["p1"]["pq"], v["p1"]["po"], v["p1"]["opt"]), (4, 0, 4, True, 4440))
        self.assertEqual(v["own1"]["qty"], 5)                                              # без предложения — как было
        self.assertEqual(v["p1"]["rtl"], 6000)                                             # розничная цена не меняется

    def order(self, ship, city="Алматы", qty=3):
        d = {"name": "Опт", "phone": "+77012345678", "city": city, "shipping": ship, "address": "Абая 10", "items": [{"id": "p1", "qty": qty}, {"id": "own1", "qty": 1}]}
        return oh.order_core(d, "k" + ship + city + str(len(self.posted)), "1.1.1.1", "cid-1")

    def test_order_courier_almaty_marks_supplier(self):
        self.c.put("/admin/api/suppliers/settings", json={"live": True})
        res, st = self.order("courier")
        self.assertEqual(st, 200, res)
        pos = self.posted[-1]["positions"]
        self.assertEqual([(p["quantity"], p["reserve"]) for p in pos[:2]], [(3, 0), (1, 1)])   # у поставщика — без резерва у нас
        self.assertIn("У поставщика: Anua тонер — 3 шт (у поставщика Сосед)", self.posted[-1]["description"])
        self.assertTrue(any("Забрать у поставщиков" in t and "Сосед: Anua тонер × 3" in t for t in self.tg))

    def test_pickup_or_region_only_own_stock(self):
        self.c.put("/admin/api/suppliers/settings", json={"live": True})
        res, st = self.order("pickup")
        self.assertEqual(st, 200, res)
        self.assertEqual([p["assortment"]["meta"]["href"].rsplit("/", 1)[-1] for p in self.posted[-1]["positions"]][:1], ["own1"])
        self.assertNotIn("p1", json.dumps(self.posted[-1]["positions"]))
        self.assertNotIn("У поставщика", self.posted[-1]["description"])
        self.order("courier", city="Астана")
        self.assertNotIn("У поставщика", self.posted[-1]["description"])

    def test_live_off_no_supplier_goods(self):
        self.order("courier")
        self.assertNotIn("У поставщика", self.posted[-1]["description"])


if __name__ == "__main__":
    unittest.main()
