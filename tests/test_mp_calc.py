"""Калькулятор прибыли WB и Ozon: расчёт сверху вниз и минимальная цена; курс Нацбанка; закупка в валюте и своя себестоимость;
ставки из кабинетов (заглушки Ozon и WB); без ключей — «нет данных из кабинета», ничего не выдумываем."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402
import marketplaces as mpk  # noqa: E402
import mp_calc  # noqa: E402
import order_hook as oh  # noqa: E402
import test_marketplaces as tm  # noqa: E402
CAT, Resp = tm.CAT, tm.Resp

NBK_XML = b"""<?xml version="1.0" encoding="utf-8"?><rates><date>28.10.2026</date>
<item><title>USD</title><description>470.5</description><quant>1</quant></item>
<item><title>RUB</title><description>5.9</description><quant>1</quant></item>
<item><title>KRW</title><description>33.4</description><quant>100</quant></item></rates>"""


class Math(unittest.TestCase):
    def test_top_down_and_min_price(self):
        r = mp_calc.calc(10000, {"commission": 15, "tax": 3, "ads": 5}, {"logistics": 500, "processing": 100, "acquiring": 150},
                         4000, 20)
        sums = {x["key"]: x["sum"] for x in r["lines"]}
        self.assertEqual(sums, {"commission": 1500, "logistics": 500, "processing": 100, "acquiring": 150, "tax": 300, "ads": 500,
                                "cost": 4000})
        self.assertEqual((r["profit"], r["margin"], r["complete"]), (2950, 29.5, True))
        self.assertEqual(r["minPrice"], round((500 + 100 + 150 + 4000) / (1 - 0.43)))    # при ней маржа ровно 20 %
        p = r["minPrice"]
        self.assertAlmostEqual(mp_calc.calc(p, {"commission": 15, "tax": 3, "ads": 5},
                                            {"logistics": 500, "processing": 100, "acquiring": 150}, 4000, 20)["margin"], 20, delta=0.1)

    def test_missing_lines_marked(self):
        r = mp_calc.calc(10000, {"commission": 15}, {"logistics": None}, None, 20)
        self.assertFalse(r["complete"])
        self.assertIsNone({x["key"]: x["sum"] for x in r["lines"]}["logistics"])


class Rates(unittest.TestCase):
    def setUp(self):
        oh._cache.pop("nbk_rates", None)
        self.addCleanup(lambda: oh._cache.pop("nbk_rates", None))

    def test_nbk_parse_and_convert(self):
        old = mp_calc.requests.get
        mp_calc.requests.get = lambda url, **k: type("R", (), {"content": NBK_XML, "raise_for_status": lambda s: None})()
        self.addCleanup(lambda: setattr(mp_calc.requests, "get", old))
        nb = mp_calc.nbk_rates()
        self.assertEqual((nb["date"], nb["rates"]["USD"], round(nb["rates"]["KRW"], 3)), ("28.10.2026", 470.5, 0.334))
        self.assertEqual(round(mp_calc.to_kzt(12.51, "USD")[0]), 5886)
        self.assertIn("НБ РК 28.10.2026", mp_calc.to_kzt(100, "RUB")[1])
        self.assertEqual(mp_calc.to_kzt(100, "KZT"), (100.0, ""))

    def test_nbk_down_uses_moysklad_rate(self):
        old = mp_calc.requests.get
        mp_calc.requests.get = lambda url, **k: (_ for _ in ()).throw(mp_calc.requests.ConnectionError("нет"))
        self.addCleanup(lambda: setattr(mp_calc.requests, "get", old))
        v, note = mp_calc.to_kzt(10, "USD", 472.9)
        self.assertEqual((round(v), "курс МойСклад" in note), (4729, True))
        self.assertIsNone(mp_calc.to_kzt(10, "EUR")[0])


class FakeOzonPrices:
    def __init__(self, base):
        self.base = base

    def __call__(self, url, json=None, headers=None, timeout=None):
        if url.endswith("/v5/product/info/prices"):
            assert json["filter"]["product_id"] == ["11"]
            return Resp(200, {"items": [{"product_id": 11, "acquiring": 2.5, "price": {"price": "1500", "currency_code": "RUB"},
                                         "commissions": {"sales_percent_fbs": 12, "fbs_direct_flow_trans_max_amount": 30,
                                                         "fbs_deliv_to_customer_amount": 25, "fbs_first_mile_max_amount": 20}}]})
        return self.base(url, json=json, headers=headers, timeout=timeout)


class FakeWBTariffs:
    def __init__(self, base):
        self.base = base

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None):
        if "/api/v2/list/goods/filter" in url:
            assert params["filterNmID"] == 1
            return Resp(200, {"data": {"listGoods": [{"nmID": 1, "currencyIsoCode4217": "KZT",
                                                      "sizes": [{"sizeID": 501, "price": 12000, "discountedPrice": 9900}]}]}})
        if "/api/v1/tariffs/commission" in url:
            return Resp(200, {"report": [{"subjectID": 7, "subjectName": "Кремы", "kgvpMarketplace": 19.5}]})
        if "/api/v1/tariffs/box" in url:
            return Resp(200, {"response": {"data": {"currency": "RUB", "warehouseList": [
                {"warehouseName": "Коледино", "geoName": "Центральный", "boxDeliveryMarketplaceBase": "80", "boxDeliveryMarketplaceLiter": "20"},
                {"warehouseName": "Алматы Атакент", "geoName": "Казахстан", "boxDeliveryMarketplaceBase": "46,5",
                 "boxDeliveryMarketplaceLiter": "10"}]}}})
        if "/content/v2/get/cards/list" in url:
            return Resp(200, {"cards": [{"nmID": 1, "subjectID": 7, "vendorCode": "00615", "dimensions": {"length": 10, "width": 10, "height": 20},
                                         "sizes": [{"chrtID": 501, "skus": ["8809640738135"]}]}], "cursor": {"total": 1}})
        return self.base(method, url, params=params, json=json, headers=headers, timeout=timeout)


class Calculator(unittest.TestCase):
    patch = tm.Channels.patch

    def setUp(self):
        tm.Channels.setUp(self)                                                         # те же заглушки Ozon, WB и МойСклад
        for k in ("nbk_rates", "mpc_prod:p1", "mpc_box:wb", "mpc_kgvp:wb"):
            oh._cache.pop(k, None)
        oh._cache["nbk_rates"] = (9e18, {"date": "28.10.2026", "rates": {"USD": 470.0, "RUB": 6.0}})
        self.addCleanup(lambda: [oh._cache.pop(k, None) for k in ("nbk_rates", "mpc_prod:p1", "mpc_box:wb", "mpc_kgvp:wb")])
        for k in mp_calc.SETTINGS.values():
            mpk.set_setting(k, "")
        mpk.set_setting("mp_cost_p1", "")
        self.patch(mpk.requests, "post", FakeOzonPrices(self.oz))
        self.patch(mpk.requests, "request", FakeWBTariffs(self.wb))
        ms = oh.ms

        def ms2(method, path, **kw):
            if path == "/entity/product/p1":
                return {"id": "p1", "name": "Крем Anua", "code": "00615", "buyPrice": {"value": 1251, "currency": {"isoCode": "USD", "rate": 472.9}}}
            return ms(method, path, **kw)
        self.patch(oh, "ms", ms2)
        import admin
        self.patch(admin, "who", lambda: {"role": "owner", "perms": []})
        app = Flask(__name__)
        app.register_blueprint(mpk.bp)
        self.c = app.test_client()

    def get(self, **q):
        return self.c.get("/admin/api/mp/calc", query_string=q).get_json()

    def test_search(self):
        self.assertEqual([x["code"] for x in self.get(q="00615")["items"]], ["00615"])

    def test_cabinet_data(self):
        self.patch(mpk, "_catalog", lambda: CAT)
        mpk.set_setting("mp_calc_tax", "3")
        j = self.get(id="p1")
        self.assertEqual((j["product"]["cost"], j["product"]["buyCur"]), (round(12.51 * 470), "USD"))
        ch = {x["id"]: x for x in j["channels"]}
        oz = ch["ozon"]
        self.assertEqual(oz["price"], 9000)                                              # 1500 ₽ × 6
        s = {x["key"]: x["sum"] for x in oz["lines"]}
        self.assertEqual((s["commission"], s["logistics"], s["processing"], s["acquiring"], s["tax"], s["ads"]),
                         (1080, 330, 120, 15, 270, None))
        self.assertIn("задайте рекламу", {x["key"]: x["note"] for x in oz["lines"]}["ads"])
        wb = ch["wb"]
        s = {x["key"]: x["sum"] for x in wb["lines"]}
        self.assertEqual(wb["price"], 9900)
        self.assertEqual(s["commission"], round(9900 * 0.195))
        self.assertEqual(s["logistics"], round((46.5 + 1 * 10) * 6))                    # 2 л: первый + один доп., склад Алматы
        self.assertIsNone(s["processing"])
        self.assertIn("Алматы Атакент", {x["key"]: x["note"] for x in wb["lines"]}["logistics"])
        self.assertFalse(wb["complete"])

    def test_own_cost_and_price(self):
        self.patch(mpk, "_catalog", lambda: CAT)
        r = self.c.post("/admin/api/mp/calc/settings", json={"id": "p1", "cost": 5000, "ads": 5, "wbacq": 1.5, "margin": 25}).get_json()
        self.assertEqual((r["settings"]["ads"], r["settings"]["margin"]), (5, 25))
        j = self.get(id="p1", prices=json.dumps({"wb": 12000}))
        self.assertEqual((j["product"]["cost"], j["product"]["costNote"]), (5000, "себестоимость задана вручную"))
        wb = next(x for x in j["channels"] if x["id"] == "wb")
        self.assertEqual((wb["price"], wb["cabinetPrice"]), (12000, 9900))
        self.assertEqual({x["key"]: x["sum"] for x in wb["lines"]}["acquiring"], 180)
        self.assertEqual(self.c.post("/admin/api/mp/calc/settings", json={"tax": "много"}).status_code, 400)

    def test_no_keys_no_invented_rates(self):
        for k in ("OZON_CLIENT_ID", "OZON_API_KEY", "WB_API_TOKEN"):
            os.environ.pop(k)
        j = self.get(id="p1")
        self.assertTrue(all(x["error"].startswith("нет данных из кабинета") and "lines" not in x for x in j["channels"]))
        self.assertIn("WB_API_TOKEN", j["channels"][-1]["error"])


if __name__ == "__main__":
    unittest.main()
