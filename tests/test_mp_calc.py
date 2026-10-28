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


MIG_HTML = """<table><tbody>
<tr><td class="buy delta-neutral">476.50</td><td class="currency">USD</td><td class="sell delta-neutral">480.00</td></tr>
<tr><td class="buy delta-plus">540.10</td><td class="currency">EUR</td><td class="sell delta-minus">545.90</td></tr>
<tr><td class="buy delta-neutral">5.70</td><td class="currency">RUB</td><td class="sell delta-neutral">5.95</td></tr>
</tbody></table>"""


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
        for k in ("nbk_rates", "mig_rates"):
            oh._cache.pop(k, None)
        self.addCleanup(lambda: [oh._cache.pop(k, None) for k in ("nbk_rates", "mig_rates")])

    def test_mig_first(self):
        self.assertEqual(mp_calc.parse_mig(MIG_HTML), {"USD": 480.0, "EUR": 545.9, "RUB": 5.95})
        old = mp_calc.requests.get
        mp_calc.requests.get = lambda url, **k: type("R", (), {"text": MIG_HTML, "raise_for_status": lambda s: None})()
        self.addCleanup(lambda: setattr(mp_calc.requests, "get", old))
        v, note = mp_calc.to_kzt(12.51, "USD")
        self.assertEqual(round(v), 6005)                                                 # Anua 00615: около 6 000 ₸
        self.assertIn("курс продажи МиГ", note)

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


WB_FIN = [{"rrdId": 1, "nmId": 1, "docTypeName": "Продажа", "quantity": 1, "retailPriceWithDisc": "9900", "commissionPercent": 19,
           "acquiringFee": "150", "deliveryService": "0", "paidAcceptance": "0", "currency": "KZT", "rrDate": "2026-09-01"},
          {"rrdId": 2, "nmId": 1, "docTypeName": "Продажа", "quantity": 1, "retailPriceWithDisc": "9900", "commissionPercent": 19,
           "acquiringFee": "150", "deliveryService": "0", "paidAcceptance": "20", "currency": "KZT", "rrDate": "2026-10-20"},
          {"rrdId": 3, "nmId": 1, "docTypeName": "", "quantity": 0, "retailPriceWithDisc": "0", "commissionPercent": 0,
           "acquiringFee": "0", "deliveryService": "400", "paidAcceptance": "0", "currency": "KZT", "rrDate": "2026-10-21"}]


class FakeWBTariffs:
    def __init__(self, base):
        self.base, self.fin, self.fin_code = base, list(WB_FIN), 200
        self.camps, self.stats, self.orders = [], [], []

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
        if "/adv/v1/promotion/count" in url:
            return Resp(200, {"adverts": [{"type": 9, "status": 9, "advert_list": [{"advertId": i} for i in self.camps]}] if self.camps else []})
        if "/adv/v3/fullstats" in url:
            assert params["ids"] and params["beginDate"] <= params["endDate"]
            return Resp(200, self.stats)
        if "/api/v1/supplier/orders" in url:
            return Resp(200, self.orders)
        if "/api/finance/v1/sales-reports/detailed" in url:
            assert json["fields"] and json["period"] == "weekly"
            if self.fin_code != 200:
                return Resp(self.fin_code, {"title": "unauthorized", "detail": "token scope not allowed"})
            return Resp(200, [x for x in self.fin if x["rrdId"] > json["rrdId"]]) if json["rrdId"] == 0 else Resp(204, None)
        if "/content/v2/get/cards/list" in url:
            return Resp(200, {"cards": [{"nmID": 1, "subjectID": 7, "vendorCode": "00615", "dimensions": {"length": 10, "width": 10, "height": 20},
                                         "sizes": [{"chrtID": 501, "skus": ["8809640738135"]}]}], "cursor": {"total": 1}})
        return self.base(method, url, params=params, json=json, headers=headers, timeout=timeout)


class Calculator(unittest.TestCase):
    patch = tm.Channels.patch

    def setUp(self):
        tm.Channels.setUp(self)                                                         # те же заглушки Ozon, WB и МойСклад
        for k in ("nbk_rates", "mpc_prod:p1", "mpc_box:wb", "mpc_kgvp:wb", "mpc_facts:wb", "mpc_facts:ozon", "mpc_ads:wb", "mpc_ads:ozon"):
            oh._cache.pop(k, None)
        self.patch(mp_calc, "SYNC_FACTS", True)
        self.addCleanup(lambda: [oh._cache.pop(k, None) for k in ("mpc_facts:wb", "mpc_facts:ozon", "mpc_ads:wb", "mpc_ads:ozon")])
        oh._cache["nbk_rates"] = (9e18, {"date": "28.10.2026", "rates": {"USD": 470.0, "RUB": 6.0}})
        self.patch(mp_calc, "mig_rates", lambda: (_ for _ in ()).throw(ValueError("МиГ в тестах выключен")))
        self.addCleanup(lambda: [oh._cache.pop(k, None) for k in ("nbk_rates", "mpc_prod:p1", "mpc_box:wb", "mpc_kgvp:wb")])
        for k in mp_calc.SETTINGS.values():
            mpk.set_setting(k, "")
        mpk.set_setting("mp_cost_p1", "")
        self.patch(mpk.requests, "post", FakeOzonPrices(self.oz))
        self.patch(mpk.requests, "request", FakeWBTariffs(self.wb))
        ms = oh.ms

        def ms2(method, path, **kw):
            if path == "/entity/product/p1":
                return {"id": "p1", "name": "Крем Anua", "code": "00615", "barcodes": [{"ean13": "8809640738135"}],
                        "buyPrice": {"value": 1251, "currency": {"isoCode": "USD", "rate": 472.9}}}
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
                         (1080, 330, 120, 15, 270, 900))                                 # реклама — план ДРР 10 % по умолчанию
        self.assertIn("план ДРР 10 %; нет ключей Ozon Performance", {x["key"]: x["note"] for x in oz["lines"]}["ads"])
        wb = ch["wb"]
        s, n = {x["key"]: x["sum"] for x in wb["lines"]}, {x["key"]: x["note"] for x in wb["lines"]}
        self.assertEqual(wb["price"], 9900)
        self.assertEqual(s["commission"], round(9900 * 0.195))                          # тариф на сегодня
        self.assertEqual(s["logistics"], round((46.5 + 1 * 10) * 6))                    # 2 л: первый + один доп., склад Алматы
        self.assertEqual((s["acquiring"], s["processing"]), (150, 10))                  # отчёт реализации: 300/19800, приёмка 20/2
        self.assertIn("Алматы Атакент", n["logistics"])
        self.assertIn("факт: 200 ₸ на продажу", n["logistics"])
        self.assertIn("отчёт реализации WB 01.09–21.10, среднее по товару (2 продаж)", n["acquiring"])
        self.assertIn("тариф WB на", n["commission"])
        self.assertTrue(wb["complete"])
        self.assertIn("план ДРР 10 %; рекламы по товару", n["ads"])

    def test_own_cost_and_price(self):
        self.patch(mpk, "_catalog", lambda: CAT)
        r = self.c.post("/admin/api/mp/calc/settings", json={"id": "p1", "cost": 5000, "ads": 5, "margin": 25}).get_json()
        self.assertEqual((r["settings"]["ads"], r["settings"]["margin"]), (5, 25))
        j = self.get(id="p1", prices=json.dumps({"wb": 12000}))
        self.assertEqual((j["product"]["cost"], j["product"]["costNote"]), (5000, "себестоимость задана вручную"))
        wb = next(x for x in j["channels"] if x["id"] == "wb")
        self.assertEqual((wb["price"], wb["cabinetPrice"]), (12000, 9900))
        self.assertEqual({x["key"]: x["sum"] for x in wb["lines"]}["acquiring"], round(12000 * 300 / 19800))
        self.assertNotIn("wbacq", r["settings"])                                         # эквайринг WB вручную больше не задаётся
        self.assertEqual(self.c.post("/admin/api/mp/calc/settings", json={"tax": "много"}).status_code, 400)

    def wb_notes(self):
        wb = next(x for x in self.get(id="p1")["channels"] if x["id"] == "wb")
        return {x["key"]: x["sum"] for x in wb["lines"]}, {x["key"]: x["note"] for x in wb["lines"]}

    def test_wb_token_without_finance(self):
        self.patch(mpk, "_catalog", lambda: CAT)
        self.patch(mpk.requests, "request", w := FakeWBTariffs(self.wb))
        w.fin_code = 401
        s, n = self.wb_notes()
        self.assertEqual((s["acquiring"], s["processing"]), (None, None))
        self.assertEqual(n["acquiring"], "у токена WB нет категории «Финансы»")

    def test_wb_wait_first_sales(self):
        self.patch(mpk, "_catalog", lambda: CAT)
        self.patch(mpk.requests, "request", w := FakeWBTariffs(self.wb))
        w.fin = []
        s, n = self.wb_notes()
        self.assertEqual((s["acquiring"], n["acquiring"], n["processing"]), (None, "ждём первые продажи", "ждём первые продажи"))

    def test_wb_cabinet_average(self):
        self.patch(mpk, "_catalog", lambda: CAT)
        self.patch(mpk.requests, "request", w := FakeWBTariffs(self.wb))
        w.fin = [dict(x, nmId=99) for x in WB_FIN]                                     # продажи другого товара
        s, n = self.wb_notes()
        self.assertEqual(s["acquiring"], 150)
        self.assertIn("среднее по кабинету", n["acquiring"])

    def test_ozon_fact_in_note(self):
        self.patch(mpk, "_catalog", lambda: CAT)
        base = mpk.requests.post

        def oz(url, json=None, headers=None, timeout=None):
            if url.endswith("/v3/product/info/list"):
                return Resp(200, {"items": [{"id": 11, "offer_id": "anua-cream", "barcodes": ["8809640738135"], "sources": [{"sku": 5011}]}]})
            if url.endswith("/v3/finance/transaction/list"):
                ops = [{"operation_type": "OperationAgentDeliveredToCustomer", "accruals_for_sale": 1500, "sale_commission": -180,
                        "amount": 1200, "operation_date": "2026-10-01 10:00:00", "items": [{"sku": 5011}],
                        "services": [{"name": "MarketplaceServiceItemDelivToCustomer", "price": -40},
                                     {"name": "MarketplaceServiceItemDirectFlowLogistic", "price": -20},
                                     {"name": "MarketplaceServiceItemDropoffSC", "price": -10}]},
                       {"operation_type": "MarketplaceRedistributionOfAcquiringOperation", "accruals_for_sale": 0, "sale_commission": 0,
                        "amount": -22.5, "operation_date": "2026-10-01 10:00:00", "items": [{"sku": 5011}], "services": []}]
                return Resp(200, {"result": {"operations": ops if json["page"] == 1 and json["filter"]["date"]["to"] > "2026" and not getattr(oz, "done", 0) else [],
                                             "page_count": 1}})
            return base(url, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "post", oz)
        oh._cache.pop("mp_map_ozon", None)
        j = self.get(id="p1")
        n = {x["key"]: x["note"] for x in next(x for x in j["channels"] if x["id"] == "ozon")["lines"]}
        self.assertIn("тариф Ozon на", n["commission"])
        self.assertIn("факт: транзакции Ozon", n["commission"])
        self.assertIn("комиссия 12.0 %", n["commission"])                               # 180 / 1500
        self.assertIn("эквайринг 1.5 %", n["commission"])                               # 22.5 / 1500

    def test_wb_fact_drr(self):
        self.patch(mpk, "_catalog", lambda: CAT)
        self.patch(mpk.requests, "request", w := FakeWBTariffs(self.wb))
        today = mp_calc.datetime.now(mpk.ALMATY)
        day = lambda k: (today - mp_calc.timedelta(days=k)).strftime("%Y-%m-%d")
        w.camps = [5]
        w.stats = [{"advertId": 5, "currency": "KZT", "days": [
            {"date": day(2) + "T00:00:00Z", "apps": [{"nms": [{"nmId": 1, "sum": 300}, {"nmId": 2, "sum": 999}]}]},
            {"date": day(20) + "T00:00:00Z", "apps": [{"nms": [{"nmId": 1, "sum": 600}]}]}]}]
        w.orders = [{"nmId": 1, "date": day(1) + "T10:00:00", "priceWithDisc": 3000, "isCancel": False},
                    {"nmId": 1, "date": day(15) + "T10:00:00", "priceWithDisc": 6000, "isCancel": False},
                    {"nmId": 1, "date": day(3) + "T10:00:00", "priceWithDisc": 5000, "isCancel": True}]
        mpk.set_setting("mp_calc_ads", "7")
        s, n = self.wb_notes()
        self.assertEqual(s["ads"], round(9900 * 0.10))                                  # 900 / 9000 — факт за 30 дней
        self.assertIn("факт ДРР: 7 дн. 10.0 %, 30 дн. 10.0 %", n["ads"])
        self.assertIn("план 7 %", n["ads"])

    def test_wb_ads_no_promotion_category(self):
        self.patch(mpk, "_catalog", lambda: CAT)
        self.patch(mpk.requests, "request", w := FakeWBTariffs(self.wb))
        orig = w.__class__.__call__
        self.patch(w.__class__, "__call__", lambda me, m, url, **k: Resp(401, {"title": "x"}) if "promotion/count" in url else orig(me, m, url, **k))
        s, n = self.wb_notes()
        self.assertEqual(s["ads"], round(9900 * 0.10))
        self.assertIn("план ДРР 10 %; у токена WB нет категории «Продвижение»", n["ads"])

    def test_ozon_perf_drr(self):
        self.patch(mpk, "_catalog", lambda: CAT)
        os.environ.update(OZON_PERF_CLIENT_ID="pc", OZON_PERF_CLIENT_SECRET="ps")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("OZON_PERF_CLIENT_ID", "OZON_PERF_CLIENT_SECRET")])
        oh._cache.pop("perf_tok:ozon", None)
        self.addCleanup(lambda: oh._cache.pop("perf_tok:ozon", None))
        base = mpk.requests.post
        today = mp_calc.datetime.now(mpk.ALMATY).strftime("%Y-%m-%d")

        def post(url, json=None, headers=None, timeout=None):
            if url.endswith("/api/client/token"):
                assert json == {"client_id": "pc", "client_secret": "ps", "grant_type": "client_credentials"}
                return Resp(200, {"access_token": "T"})
            if url.endswith("/v3/product/info/list"):
                return Resp(200, {"items": [{"id": 11, "offer_id": "anua-cream", "barcodes": ["8809640738135"], "sources": [{"sku": 5011}]}]})
            if url.endswith("/v1/analytics/data"):
                return Resp(200, {"result": {"data": [{"dimensions": [{"id": "5011"}], "metrics": [20000]}]}})
            if url.endswith("/v3/finance/transaction/list"):
                return Resp(200, {"result": {"operations": [], "page_count": 1}})
            return base(url, json=json, headers=headers, timeout=timeout)

        def get(url, params=None, timeout=None, headers=None):
            assert headers["Authorization"] == "Bearer T"
            if url.endswith("/api/client/campaign"):
                return Resp(200, {"list": [{"id": "77", "title": "Крем"}]})
            if url.endswith("/api/client/campaign/77/v2/products"):
                return Resp(200, {"products": [{"sku": "5011"}, {"sku": "5012"}]})
            if url.endswith("/api/client/statistics/expense/json"):
                return Resp(200, {"rows": [{"id": "77", "date": today, "moneySpent": "3000,00", "bonusSpent": "0"}]})
            raise AssertionError(url)
        self.patch(mpk.requests, "post", post)
        self.patch(mp_calc.requests, "get", get)
        oh._cache.pop("mp_map_ozon", None)
        j = self.get(id="p1")
        oz = next(x for x in j["channels"] if x["id"] == "ozon")
        s, n = {x["key"]: x["sum"] for x in oz["lines"]}, {x["key"]: x["note"] for x in oz["lines"]}
        self.assertEqual(s["ads"], round(9000 * 0.075))                                 # 3000 / 2 товара / 20000
        self.assertIn("30 дн. 7.5 %", n["ads"])
        self.assertIn("делится поровну", n["ads"])

    def test_no_keys_no_invented_rates(self):
        for k in ("OZON_CLIENT_ID", "OZON_API_KEY", "WB_API_TOKEN"):
            os.environ.pop(k)
        j = self.get(id="p1")
        self.assertTrue(all(x["error"].startswith("нет данных из кабинета") and "lines" not in x for x in j["channels"]))
        self.assertIn("WB_API_TOKEN", j["channels"][-1]["error"])


class FakeWBPrices(FakeWBTariffs):
    def __init__(self, base, cur="KZT"):
        super().__init__(base)
        self.cur, self.uploads, self.buffer = cur, [], True

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None):
        if "/api/v2/list/goods/filter" in url:
            return Resp(200, {"data": {"listGoods": [{"nmID": params["filterNmID"], "currencyIsoCode4217": self.cur,
                                                      "sizes": [{"sizeID": 501, "price": 12000, "discountedPrice": 9900}]}]}})
        if url.endswith("/api/v2/upload/task"):
            self.uploads.append(json)
            return Resp(200, {"data": {"id": 777, "alreadyExists": False}, "error": False})
        if "/api/v2/buffer/tasks" in url:
            return Resp(200, {"data": {"uploadID": 777, "status": 1}} if self.buffer else {"data": None})
        if "/api/v2/history/tasks" in url:
            return Resp(200, {"data": {"uploadID": 777, "status": 3}})
        return super().__call__(method, url, params=params, json=json, headers=headers, timeout=timeout)


class SetPrice(Calculator):
    def setUp(self):
        super().setUp()
        self.patch(mpk, "_catalog", lambda: CAT)
        self.w = FakeWBPrices(self.wb)
        self.patch(mpk.requests, "request", self.w)
        mpk.set_setting("mp_price_wb_p1", "")

    def post(self, **b):
        return self.c.post("/admin/api/mp/calc/price", json=b)

    def test_set_price_and_status(self):
        r = self.post(id="p1", shop="wb", price=13500).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(self.w.uploads, [{"data": [{"nmID": 1, "price": 13500, "discount": 0}]}])   # без скидки продавца
        st = self.c.get("/admin/api/mp/calc/price", query_string={"id": "p1", "shop": "wb"}).get_json()["task"]
        self.assertEqual((st["id"], st["status"]), (777, "в обработке"))
        self.w.buffer = False
        st = self.c.get("/admin/api/mp/calc/price", query_string={"id": "p1", "shop": "wb"}).get_json()["task"]
        self.assertEqual((st["status"], st["final"]), ("цена обновилась", True))
        j = self.get(id="p1")
        self.assertEqual(next(x for x in j["channels"] if x["id"] == "wb")["priceTask"]["price"], 13500)

    def test_not_tenge_not_set(self):
        self.w.cur = "RUB"
        r = self.post(id="p1", shop="wb", price=13500)
        self.assertEqual(r.status_code, 409)
        self.assertIn("RUB", r.get_json()["error"])
        self.assertEqual(self.w.uploads, [])

    def test_no_card_nothing_created(self):
        self.patch(mpk, "_catalog", lambda: {})                                         # пары нет, и по штрихкоду карточки нет
        self.patch(mp_calc, "_ref_for", lambda ch, pid: (None, {"products": {}}))
        old = self.w.__class__.__call__

        def no_cards(me, method, url, **k):
            if "/content/v2/get/cards/list" in url:
                return Resp(200, {"cards": [], "cursor": {"total": 0}})
            return old(me, method, url, **k)
        self.patch(self.w.__class__, "__call__", no_cards)
        r = self.post(id="p1", shop="wb", price=13500)
        self.assertEqual(r.status_code, 409)
        self.assertIn("нет карточки", r.get_json()["error"])
        self.assertEqual(self.w.uploads, [])

    def test_owner_only_and_checks(self):
        import admin
        self.assertEqual(self.post(id="p1", shop="ozon", price=100).status_code, 400)    # только WB
        self.assertEqual(self.post(id="p1", shop="wb", price=0).status_code, 400)
        self.patch(admin, "who", lambda: {"role": "manager", "perms": ["orders"]})
        self.assertEqual(self.post(id="p1", shop="wb", price=13500).status_code, 403)
        self.assertEqual(self.w.uploads, [])


if __name__ == "__main__":
    unittest.main()
