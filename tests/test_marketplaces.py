"""Каналы продаж (Ozon, WB): выключено по умолчанию; новый заказ → заказ МойСклад с пометкой один раз; товар без пары — тревога,
заказ не создаём; заказы до включения не переносим; отмена → «Отменен» и резерв снят; остатки минус запас уходят на склад.
Ozon, WB и МойСклад здесь — заглушки."""
import os
import sys
import tempfile
import time
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402
import marketplaces as mpk  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402

CAT = {"p1": {"id": "p1", "name": "Крем Anua", "code": "00615", "article": "", "barcode": "8809640738135", "qty": 10},
       "p2": {"id": "p2", "name": "Тонер", "code": "00700", "article": "TON-1", "barcode": "", "qty": 3}}
NOW = time.time()


def iso(t):
    return mpk._iso(t)


class Resp:
    def __init__(self, code, body):
        self.status_code, self.body = code, body

    def json(self):
        return self.body


class FakeOzon:
    def __init__(self):
        self.postings, self.stocks = [], []

    def __call__(self, url, json=None, headers=None, timeout=None):
        path = url[len(mpk.CHANNELS["ozon"].api()):]
        assert headers["Client-Id"] == "cid" and headers["Api-Key"] == "key"
        if path == "/v4/posting/fbs/list":
            return Resp(200, {"postings": self.postings, "has_next": False, "cursor": ""})
        if path == "/v3/product/list":
            return Resp(200, {"result": {"items": [{"product_id": 11}, {"product_id": 12}, {"product_id": 13}], "last_id": ""}})
        if path == "/v3/product/info/list":
            return Resp(200, {"items": [{"id": 11, "offer_id": "anua-cream", "barcodes": ["8809640738135"]},
                                        {"id": 12, "offer_id": "TON-1", "barcodes": []},
                                        {"id": 13, "offer_id": "chuzhoy", "barcodes": [], "name": "Без пары"}]})
        if path == "/v2/warehouse/list":
            return Resp(200, {"warehouses": [{"warehouse_id": 777, "status": "created"}]})
        if path == "/v2/products/stocks":
            self.stocks.append(json["stocks"])
            return Resp(200, {"result": [{"offer_id": s["offer_id"], "updated": True, "errors": []} for s in json["stocks"]]})
        raise AssertionError(path)


class FakeWB:
    def __init__(self):
        self.orders, self.status, self.stocks = [], {}, []

    def __call__(self, method, url, params=None, json=None, headers=None, timeout=None):
        assert headers["Authorization"] == "tok"
        path = url.split(".wildberries.ru", 1)[1]
        if path == "/api/v3/orders":
            return Resp(200, {"next": 0, "orders": self.orders})
        if path == "/api/v3/orders/status":
            return Resp(200, {"orders": [dict(self.status.get(i, {"supplierStatus": "new", "wbStatus": "waiting"}), id=i)
                                         for i in json["orders"]]})
        if path == "/content/v2/get/cards/list":
            return Resp(200, {"cards": [{"nmID": 1, "vendorCode": "00615", "sizes": [{"chrtID": 501, "skus": ["8809640738135"]}]}],
                              "cursor": {"total": 1}})
        if path == "/api/v3/warehouses":
            return Resp(200, [{"id": 9001, "name": "Алматы"}])
        if path.startswith("/api/v3/stocks/"):
            self.stocks.append((path, json["stocks"]))
            return Resp(204, None)
        raise AssertionError((method, path))


class Channels(unittest.TestCase):
    def setUp(self):
        os.environ.update(OZON_CLIENT_ID="cid", OZON_API_KEY="key", WB_API_TOKEN="tok")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("OZON_CLIENT_ID", "OZON_API_KEY", "WB_API_TOKEN")])
        with mpk.db() as d:
            d.run("DELETE FROM mp_order")
        for ch in mpk.CHANNELS.values():
            ch.sent.clear()
            ch.sent_at = 0
            ch.last.update(orders=0, stocks=0)
            mpk.set_setting(f"mp_{ch.key}_since", str(int(NOW - 3600)))
            mpk.set_setting(f"mp_{ch.key}_agent", f"agent-{ch.key}")
            for k in (f"mp_map_{ch.key}", f"mp_wh_{ch.key}"):
                oh._cache.pop(k, None)
        mpk.set_setting("mp_reserve", "20")
        oh._cache.pop("ms_order_states", None)
        modules.update("marketplaces", on=True)
        self.addCleanup(lambda: modules.update("marketplaces", on=False))
        self.oz, self.wb = FakeOzon(), FakeWB()
        self.patch(mpk.requests, "post", self.oz)
        self.patch(mpk.requests, "request", self.wb)
        self.patch(mpk.time, "sleep", lambda s: None)
        self.posted, self.alerts, self.msgs, self.puts = [], [], [], []

        def ms(method, path, **kw):
            if path == "/entity/customerorder/metadata":
                return {"states": [{"name": "Отменен", "meta": {"href": "https://x/state/s-cancel"}}]}
            if path == "/entity/customerorder/o1" and method == "GET":
                return {"id": "o1", "description": "Заказ с Ozon (FBS)"}
            if path == "/entity/customerorder/o1/positions":
                return {"rows": [{"id": "pos1", "reserve": 1}, {"id": "pos2", "reserve": 0}]}
            if method == "PUT":
                self.puts.append((path, kw["json"]))
                return {}
            raise AssertionError((method, path))
        for name, val in {"panel_catalog": lambda: CAT, "ms": ms, "organization": lambda: "org-1", "store_id": lambda: "store-1",
                          "notify_bg": lambda fn: None, "notify_staff": lambda key, text, **k: self.msgs.append(text),
                          "alert": lambda key, text, every=600: self.alerts.append(text),
                          "_post_order": lambda body: self.posted.append(body) or {"name": "1800", "id": "o1"}}.items():
            self.patch(oh, name, val)

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def posting(self, num="0123-0001-1", status="awaiting_packaging", at=None, products=None):
        return {"posting_number": num, "order_number": "0123-0001", "status_alias": status, "in_process_at": iso(at or NOW - 600),
                "shipment_date": iso(NOW + 86400),
                "products": products or [{"product_id": 11, "product_offer_id": "anua-cream", "product_name": "Anua крем", "quantity": 2,
                                          "price": "8690.0000", "marketplace_seller_price_currency_code": "KZT"}]}

    def test_off_by_default(self):
        self.assertFalse(modules.REGISTRY["marketplaces"].get("default"))
        modules.update("marketplaces", on=False)
        self.assertFalse(mpk.on())
        modules.update("marketplaces", on=True)
        self.assertTrue(mpk.on())
        for k in ("OZON_CLIENT_ID", "OZON_API_KEY", "WB_API_TOKEN"):
            os.environ.pop(k)
        self.assertFalse(mpk.on())                                            # без ключей на Render — ничего не делаем
        mpk.tick(NOW)
        self.assertEqual(self.posted, [])

    def test_ozon_order_once_with_mark(self):
        self.oz.postings = [self.posting()]
        ch = mpk.CHANNELS["ozon"]
        self.assertEqual(mpk.sync_orders(ch, NOW), 1)
        b = self.posted[0]
        self.assertEqual(b["externalCode"], "ozon-0123-0001-1")
        self.assertTrue(b["description"].startswith("Заказ с Ozon (FBS)\nОтправление 0123-0001-1, заказ Ozon 0123-0001"))
        self.assertIn("Собрать и сдать до:", b["description"])
        self.assertEqual(b["agent"]["meta"]["href"].rsplit("/", 1)[1], "agent-ozon")
        p = b["positions"][0]
        self.assertEqual((p["quantity"], p["price"], p["reserve"]), (2, 869000, 2))
        self.assertTrue(p["assortment"]["meta"]["href"].endswith("/p1"))
        self.assertIn("Заказ с Ozon № 1800: 2 шт на 17 380 ₸", self.msgs[0].replace(" ", " "))
        self.assertEqual(mpk.sync_orders(ch, NOW), 0)                         # повтор — второго заказа нет
        self.assertEqual(len(self.posted), 1)

    def test_ozon_unmatched_alerts_once(self):
        self.oz.postings = [self.posting(products=[{"product_id": 13, "product_offer_id": "chuzhoy", "product_name": "Без пары",
                                                    "quantity": 1, "price": "100"}])]
        ch = mpk.CHANNELS["ozon"]
        mpk.sync_orders(ch, NOW)
        mpk.sync_orders(ch, NOW)
        self.assertEqual(self.posted, [])
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("нет пары в МойСклад", self.alerts[0])
        self.assertEqual(mpk.get(ch, "0123-0001-1")["tries"], 2)
        self.assertEqual(mpk.status()["channels"][0]["lost"], [{"offer": "chuzhoy", "name": "Без пары"}])

    def test_old_and_waiting_not_moved(self):
        self.oz.postings = [self.posting("old-1", at=NOW - 7200), self.posting("wait-1", status="awaiting_registration")]
        ch = mpk.CHANNELS["ozon"]
        mpk.sync_orders(ch, NOW)
        self.assertEqual(self.posted, [])
        self.assertIn("до включения", mpk.get(ch, "old-1")["error"])
        self.oz.postings = [self.posting("wait-1")]                           # Ozon оформил — теперь создаём
        mpk.sync_orders(ch, NOW)
        self.assertEqual(len(self.posted), 1)

    def test_ozon_cancel(self):
        ch = mpk.CHANNELS["ozon"]
        self.oz.postings = [self.posting()]
        mpk.sync_orders(ch, NOW)
        self.oz.postings = [self.posting(status="cancelled")]
        mpk.sync_orders(ch, NOW)
        paths = [p for p, _ in self.puts]
        self.assertEqual(paths, ["/entity/customerorder/o1", "/entity/customerorder/o1/positions/pos1"])
        self.assertEqual(self.puts[0][1]["state"]["meta"]["href"], "https://x/state/s-cancel")
        self.assertIn("Отменён на Ozon", self.puts[0][1]["description"])
        self.assertEqual(self.puts[1][1], {"reserve": 0})
        mpk.sync_orders(ch, NOW)
        self.assertEqual(len(self.puts), 2)                                   # второй раз не отменяем

    def test_wb_order_and_cancel(self):
        ch = mpk.CHANNELS["wb"]
        self.wb.orders = [{"id": 4242, "orderUid": "u-1", "createdAt": iso(NOW - 300), "chrtId": 501, "article": "00615",
                           "skus": ["8809640738135"], "price": 869000, "convertedPrice": 869000, "currencyCode": 398,
                           "convertedCurrencyCode": 398}]
        self.assertEqual(mpk.sync_orders(ch, NOW), 1)
        b = self.posted[0]
        self.assertEqual(b["externalCode"], "wb-4242")
        self.assertTrue(b["description"].startswith("Заказ с WB (FBS)\nСборочное задание 4242"))
        self.assertEqual((b["positions"][0]["quantity"], b["positions"][0]["price"]), (1, 869000))
        self.wb.status[4242] = {"supplierStatus": "confirm", "wbStatus": "canceled_by_client"}
        mpk.sync_orders(ch, NOW)
        self.assertIn("Отменён на WB", self.puts[0][1]["description"])

    def test_stocks_with_reserve(self):
        self.assertEqual([mpk.shown(q, 20) for q in (0, 1, 3, 10, 50)], [0, 0, 2, 8, 40])
        self.assertEqual(mpk.shown(5, 0), 5)
        oz = mpk.CHANNELS["ozon"]
        r = mpk.push_stocks(oz)
        self.assertEqual(r["sent"], 2)
        sent = {s["offer_id"]: (s["stock"], s["warehouse_id"]) for s in self.oz.stocks[0]}
        self.assertEqual(sent, {"anua-cream": (8, 777), "TON-1": (2, 777)})
        self.assertEqual(mpk.push_stocks(oz)["sent"], 0)                      # не менялось — не шлём
        wb = mpk.CHANNELS["wb"]
        mpk.push_stocks(wb)
        self.assertEqual(self.wb.stocks[0], ("/api/v3/stocks/9001", [{"chrtId": 501, "amount": 8}]))

    def test_routes(self):
        import admin
        app = Flask(__name__)
        app.register_blueprint(mpk.bp)
        c = app.test_client()
        self.patch(admin, "who", lambda: {"role": "manager", "perms": []})
        self.assertEqual(c.get("/admin/api/mp/status").status_code, 403)
        self.patch(admin, "who", lambda: {"role": "owner", "perms": []})
        j = c.post("/admin/api/mp/settings", json={"reserve": 10}).get_json()
        self.assertEqual((j["ok"], j["reserve"]), (True, 10))
        self.assertEqual([x["id"] for x in j["channels"]], ["ozon", "wb"])
        self.assertEqual(c.post("/admin/api/mp/settings", json={"reserve": "много"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
