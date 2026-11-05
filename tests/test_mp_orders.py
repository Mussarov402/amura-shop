"""Заказы площадок (04.11.2026): цена покупателя в тенге (Ozon v4 — из financial_data, если в products 0; рубли — по курсу МиГ),
экономика заказа в комментарии, отгрузка МойСклад при передаче в доставку (без второй), отмена после отгрузки — возврат,
разовая правка заказов с ценой 0."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_marketplaces as tm  # noqa: E402

import marketplaces as mpk  # noqa: E402
import mp_calc  # noqa: E402
import order_hook as oh  # noqa: E402

NOW, iso = tm.NOW, tm.iso


def calc_stub(ch, pid, prod, st, price_override=None):
    p = price_override
    lines = [("commission", p * 0.2), ("logistics", 300), ("processing", 20), ("acquiring", p * 0.025),
             ("tax", p * 0.04), ("ads", 0), ("pack", 500), ("cost", 2000)]
    return {"lines": [{"key": k, "sum": round(v), "note": "приёмка №00312" if k == "cost" else ""} for k, v in lines]}


class MpOrders(unittest.TestCase):
    patch, posting = tm.Channels.patch, tm.Channels.posting           # заглушки площадок и МойСклад — как в test_marketplaces (без его тестов)

    def setUp(self):
        tm.Channels.setUp(self)
        with mpk._ship_db() as d:
            d.run("DELETE FROM mp_ship")
        with mpk._label_db() as d:
            d.run("DELETE FROM mp_label")
        self.patch(mp_calc, "channel_calc", calc_stub)
        self.patch(mp_calc, "product", lambda pid: {"cost": 2000})
        self.patch(mp_calc, "settings", lambda: {"margin": 15, "pack": 500, "tax": 4, "ads": 0})
        self.patch(mp_calc, "to_kzt", lambda v, cur, *a, **k: (float(v), "") if cur in ("KZT", "") else (float(v) * 5.5, "курс покупки МиГ: 1 RUB = 5.5 ₸"))

    def test_ozon_v4_price_from_financial_data_in_rub(self):
        p = self.posting(products=[{"product_id": 11, "sku": 9001, "offer_id": "anua-cream", "name": "Anua крем", "quantity": 1,
                                    "price": "0.0000", "currency_code": "RUB"}])
        p["financial_data"] = {"products": [{"product_id": 9001, "price": 1000, "commission_amount": 150, "payout": 850,
                                             "currency_code": "RUB"}]}
        self.oz.postings = [p]
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        b = self.posted[0]
        self.assertEqual(b["positions"][0]["price"], 550000)                        # 1000 RUB × 5,5 = 5 500 ₸ (в копейках)
        d = b["description"]
        self.assertIn("Цена в тенге: 1000 RUB", d)
        self.assertIn("Продажа: 5 500", d)
        self.assertIn("Комиссия: −825 (факт Ozon)", d)                               # 150 RUB × 5,5 — факт вместо оценки 20 %
        self.assertIn("Логистика и последняя миля: −300 (оценка)", d)
        self.assertIn("Упаковка: −500", d)
        self.assertIn("Себестоимость: −2 000 (приёмка №00312)", d)
        self.assertIn("К выплате: 4 217", d)                                        # 5500 − 825 − 300 − 20 − 138 (2,5 %)
        self.assertIn("Прибыль: 1 497, маржа 27,2 %", d)                              # − упаковка 500 − налог 220 − себестоимость 2000

    def test_ozon_v4_real_answer_04_11(self):
        """Ответ Ozon KZ по заказу № 1894 (04.11.2026, от менеджера): price — объект {amount, currency}."""
        p = self.posting(products=[{"name": "MEDI-PEEL патчи Rose", "offer_id": "anua-cream", "price": {"amount": "5790", "currency": "KZT"},
                                    "quantity": 1, "sku": 6224576836, "product_id": 11}])
        p["financial_data"] = {"products": [{"commission": {"amount": 0, "currency": "RUB", "percent": 0},
                                             "customer_price": {"amount": "1202.85", "currency": "RUB"}, "old_price": 5790, "payout": 0,
                                             "price": 5790, "product_id": 6224576836, "quantity": 1}]}
        line = mpk.CHANNELS["ozon"].norm(p)["lines"][0]
        self.assertEqual((line["price"], line["cur"]), (5790.0, "KZT"))            # не customer_price в рублях
        self.oz.postings = [p]
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        self.assertEqual(self.posted[0]["positions"][0]["price"], 579000)
        self.assertNotIn("Цена в тенге", self.posted[0]["description"])
        self.assertIn("Комиссия: −1 158 (оценка)", self.posted[0]["description"])   # факта ещё нет (0) — оценка 20 %

    def test_price_object_and_zero_alert(self):
        self.assertEqual(mpk._money({"price": "12.5", "currency_code": "KZT"}), 12.5)
        self.assertEqual(mpk._money({"units": "3", "nanos": 500000000}), 3.5)
        p = self.posting(products=[{"product_id": 11, "offer_id": "anua-cream", "quantity": 1, "price": None}])
        self.oz.postings = [p]
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        self.assertEqual(self.posted[0]["positions"][0]["price"], 0)
        self.assertTrue(any("без цены" in a for a in self.alerts))

    def test_wb_currency_number(self):
        self.assertEqual(mpk.WB.cur(643), "RUB")
        self.wb.orders = [{"id": 5, "createdAt": iso(NOW - 300), "chrtId": 501, "article": "00615", "skus": ["8809640738135"],
                           "price": 0, "convertedPrice": 200000, "currencyCode": 643, "convertedCurrencyCode": 643}]
        self.wb.status = {5: {"supplierStatus": "confirm", "wbStatus": "waiting"}}
        mpk.sync_orders(mpk.CHANNELS["wb"], NOW)
        self.assertEqual(self.posted[0]["positions"][0]["price"], 1100000)           # 2000 RUB × 5,5

    def test_ship_once_and_return_after_ship(self):
        import shipments
        calls = []
        self.patch(shipments, "create", lambda oid, who="": calls.append((oid, who)) or {"status": "sent", "msNumber": "D-7"})
        self.oz.postings = [self.posting()]
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        self.oz.postings = [self.posting(status="delivering")]
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        self.assertEqual(calls, [("o1", "Ozon: передан в доставку")])              # одна отгрузка на заказ
        self.assertEqual(mpk.shipped(mpk.CHANNELS["ozon"], "0123-0001-1"), "D-7")
        self.oz.postings = [self.posting(status="cancelled")]
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        self.assertEqual(self.puts, [])                                            # заказ не «Отменен», резерв не трогаем
        self.assertTrue(any("после отгрузки" in a for a in self.alerts))

    def test_ship_when_demand_exists(self):
        import shipments
        self.patch(shipments, "create", mock.Mock(side_effect=ValueError("По заказу уже есть отгрузка в МойСклад")))
        self.oz.postings = [self.posting()]
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        self.oz.postings = [self.posting(status="delivering")]
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        self.assertEqual(mpk.shipped(mpk.CHANNELS["ozon"], "0123-0001-1"), "уже была")

    def test_repair_zero(self):
        mpk._put(mpk.CHANNELS["ozon"], "0123-0001-1", status="awaiting_packaging", order_id="o9", number="1894")
        self.oz.postings = [self.posting()]
        old_ms = oh.ms

        def ms(method, path, **kw):
            if path == "/entity/customerorder/o9/positions":
                return {"rows": [{"id": "q1", "price": 0, "quantity": 2, "assortment": {"meta": {"href": "https://x/product/p1"}}}]}
            if path == "/entity/customerorder/o9" and method == "GET":
                return {"description": "Заказ с Ozon (FBS)\nОтправление 0123-0001-1"}
            return old_ms(method, path, **kw)
        self.patch(oh, "ms", ms)
        self.assertEqual(mpk.repair_zero(mpk.CHANNELS["ozon"], NOW), ["1894"])
        self.assertEqual(self.puts[0], ("/entity/customerorder/o9/positions/q1", {"price": 869000}))
        self.assertIn("Экономика заказа", self.puts[1][1]["description"])
        self.assertIn("была 0", self.puts[1][1]["description"])

    def _ms_files(self):
        files, old_ms = [], oh.ms

        def ms(method, path, **kw):
            if path == "/entity/customerorder/o1/files":
                if method == "POST":
                    files.append(kw["json"][0])
                    return [{}]
                return {"rows": [{"filename": f["filename"]} for f in files]}
            return old_ms(method, path, **kw)
        self.patch(oh, "ms", ms)
        return files

    def test_ozon_label_once(self):
        files = self._ms_files()
        pdf = mock.Mock(status_code=200, content=b"%PDF-1.4 label")
        orig = self.oz.__call__

        def post(url, json=None, headers=None, timeout=None):
            if url.endswith("/v2/posting/fbs/package-label"):
                self.assertEqual(json, {"posting_number": ["0123-0001-1"]})
                return pdf
            return orig(url, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "post", post)
        self.oz.postings = [self.posting()]
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        self.assertEqual(files, [])                                                # ещё не собран — этикетки нет
        self.oz.postings = [self.posting(status="awaiting_deliver")]
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        mpk.sync_orders(mpk.CHANNELS["ozon"], NOW)
        self.assertEqual([f["filename"] for f in files], ["Этикетка Ozon 0123-0001-1.pdf"])
        import base64
        self.assertEqual(base64.b64decode(files[0]["content"]), b"%PDF-1.4 label")
        self.assertEqual(self.puts[-1][1]["description"].splitlines()[-1], "Этикетка: прикреплена (Файлы)")

    def test_wb_sticker_png(self):
        files = self._ms_files()
        import base64
        orig = self.wb.__call__

        def req(method, url, params=None, json=None, headers=None, timeout=None):
            if url.endswith("/api/v3/orders/stickers"):
                self.assertEqual((params, json), ({"type": "png", "width": 58, "height": 40}, {"orders": [5]}))
                return tm.Resp(200, {"stickers": [{"orderId": 5, "file": base64.b64encode(b"PNG").decode()}]})
            return orig(method, url, params=params, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "request", req)
        self.wb.orders = [{"id": 5, "createdAt": iso(NOW - 300), "chrtId": 501, "article": "00615", "skus": ["8809640738135"],
                           "price": 100000, "currencyCode": 398}]
        self.wb.status = {5: {"supplierStatus": "new", "wbStatus": "waiting"}}
        mpk.sync_orders(mpk.CHANNELS["wb"], NOW)
        self.wb.status = {5: {"supplierStatus": "confirm", "wbStatus": "waiting"}}
        mpk.sync_orders(mpk.CHANNELS["wb"], NOW)
        self.assertEqual([f["filename"] for f in files], ["Стикер WB 5.png"])


if __name__ == "__main__":
    unittest.main()


class StatusFast(unittest.TestCase):
    """/mp/status — без списка товаров и без запроса в базу на каждый товар (было 32 с и 150 КБ, 04.11.2026)."""
    patch = tm.Channels.patch

    def setUp(self):
        tm.Channels.setUp(self)

    def test_kiz_once_and_no_goods_list(self):
        prods = {str(i): {"offer": f"o{i}", "name": f"Товар {i}", "archived": False, "kiz": True, "nm": i + 1} for i in range(300)}
        oh._cache["mp_map_wb"] = (9e12, {"products": prods, "ref": {}, "lost": []})
        self.addCleanup(lambda: oh._cache.pop("mp_map_wb", None))
        calls = []
        real = mpk.setting
        self.patch(mpk, "setting", lambda k, d="": calls.append(k) or real(k, d))
        st = mpk.status()
        wb = next(c for c in st["channels"] if c["id"] == "wb")
        self.assertEqual((wb["goodsN"], wb["goodsHidden"]), (300, 300))           # КИЗ выключена — все «нужна маркировка»
        self.assertNotIn("goods", wb)
        self.assertLess(calls.count("mp_kiz_on"), 10)
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(mpk.bp)
        self.patch(mpk, "_me", lambda owner_only=False: ({"role": "owner"}, None))
        g = app.test_client().get("/admin/api/mp/goods?shop=wb").get_json()["goods"]
        self.assertEqual(len(g), 300)


class StockSkip(unittest.TestCase):
    """Остатки: товар, которого на площадке ещё нет («Product is not created»), — пропуск, не ошибка выгрузки (04.11.2026)."""
    patch, posting = tm.Channels.patch, tm.Channels.posting

    def setUp(self):
        tm.Channels.setUp(self)
        orig = self.oz.__call__

        def oz(url, json=None, headers=None, timeout=None):
            if url.endswith("/v2/products/stocks"):
                return tm.Resp(200, {"result": [{"offer_id": s["offer_id"], "updated": s["offer_id"] != "TON-1",
                                                 "errors": [] if s["offer_id"] != "TON-1" else [{"message": "Product is not created"}]}
                                                for s in json["stocks"]]})
            return orig(url, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "post", oz)

    def test_skip_not_created(self):
        ch = mpk.CHANNELS["ozon"]
        r = mpk.push_stocks(ch, force=True)
        self.assertEqual((r["sent"], r["skipped"], r["errors"]), (1, ["TON-1"], []))
        ch.last.update(orders=9e12, stocks=0)
        mpk.tick(tm.NOW)
        import json
        st = json.loads(mpk.setting("mp_ozon_stocks"))
        self.assertTrue(st["ok"])                                                    # зелёный, с пометкой «пропущено 1»
