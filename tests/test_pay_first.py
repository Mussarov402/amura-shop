"""Розница с оплатой картой: заказ в МойСклад появляется только после оплаты.
Черновик → платёж Freedom по черновику → Result URL → заказ «Подтвержден» (один раз); не хватило товара — тревога, повтор."""
import json
import os
import sys
import tempfile
import time
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "fonts"), exist_ok=True)
for _f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
    if not os.path.exists(os.path.join(ROOT, "fonts", _f)):
        import shutil
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

from flask import Flask  # noqa: E402
import freedom  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402

KEY = "test-secret"
ITEM = {"id": "p1", "name": "Крем", "qty": 5, "opt": 5000, "mid": 0, "box": 0, "boxQty": 0, "rtl": 6690}


class Resp:
    def __init__(self, text):
        self.text = text


class PayFirst(unittest.TestCase):
    def setUp(self):
        os.environ.update(FREEDOM_MERCHANT_ID="555", FREEDOM_SECRET_KEY=KEY, FREEDOM_TEST="1")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FREEDOM_MERCHANT_ID", "FREEDOM_SECRET_KEY", "FREEDOM_TEST")])
        with freedom.db() as d:
            d.run("DELETE FROM card_pay")
            d.run("DELETE FROM card_draft")
        modules.update("cardpay", on=True)
        self.addCleanup(lambda: modules.update("cardpay", on=False))
        self.posted, self.alerts, self.sent, self.stock = [], [], [], 5
        oh._cache.pop("ms_order_states", None)
        self.patch(freedom.requests, "post", lambda url, data=None, timeout=None: self.sent.append(dict(data)) or Resp(
            "<?xml version='1.0'?><response><pg_status>ok</pg_status><pg_payment_id>9001</pg_payment_id>"
            "<pg_redirect_url>https://pay.example/9001</pg_redirect_url></response>"))

        def ms(method, path, **kw):
            if path == "/entity/customerorder/metadata":
                return {"states": [{"name": "Новый", "meta": {"href": "https://x/state/s-new"}}, {"name": "Подтвержден", "meta": {"href": "https://x/state/s-ok"}}]}
            if path == "/entity/customerorder/o1" and method == "GET":
                return {"id": "o1", "state": {"meta": {"href": "https://x/state/s-ok"}}, "description": ""}
            raise AssertionError((method, path))
        for name, val in {"catalog": lambda: {"p1": dict(ITEM, qty=self.stock)}, "too_many": lambda *a, **k: False, "ms": ms,
                          "organization": lambda: "org-1", "store_id": lambda: "store-1", "delivery_service_id": lambda: "dlv-1",
                          "notify_bg": lambda fn: None, "mark_retail": lambda *a: None, "tg": lambda *a, **k: None,
                          "alert": lambda kind, text: kind == "cardpay" and self.alerts.append(text),   # только оплата картой: фоновое обновление каталога шлёт свои тревоги
                          "_post_order": lambda body: self.posted.append(body) or {"name": "1700", "id": "o1"}}.items():
            self.patch(oh, name, val)
        import checkout
        self.patch(checkout, "order_delivery", lambda d, ship, city, goods: (1000, "Курьер по Алматы (Яндекс) — Абая 1", None))
        app = Flask(__name__)
        app.register_blueprint(freedom.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def checkout(self, **kw):
        d = {"name": "Айгерим", "phone": "+77012345678", "city": "Алматы", "shipping": "courier", "address": "Абая 1", "mode": "retail",
             "items": [{"id": "p1", "qty": 2}], "pay": "card", "payFirst": True, "consent": True} | kw
        return oh.order_core(d, "k" + str(time.time()), "1.1.1.1", "cid-1")

    def pay(self, token):
        r = json.loads(self.c.post("/pay/card/init", json={"token": token}).data)
        self.assertTrue(r["ok"], r)
        p = {"pg_merchant_id": "555", "pg_order_id": self.sent[-1]["pg_order_id"], "pg_payment_id": "9001", "pg_amount": self.sent[-1]["pg_amount"],
             "pg_currency": "KZT", "pg_result": "1", "pg_salt": "s"}
        p["pg_sig"] = freedom.sign("result", p, KEY)
        self.assertIn(b"<pg_status>ok</pg_status>", self.c.post("/pay/card/result", data=p).data)
        return self.sent[-1]["pg_order_id"]

    def test_draft_then_order_after_payment(self):
        res, st = self.checkout()
        self.assertEqual((st, res["total"]), (200, 2 * 6690 + 1000))
        self.assertEqual(self.posted, [])                                      # до оплаты в МойСклад ничего
        self.assertEqual(freedom.my_drafts("cid-1")[0]["total"], 14380)        # «Ожидает оплаты»
        self.assertEqual(self.c.post("/pay/card/status", json={"token": res["payToken"]}).get_json()["status"], "new")
        pg = self.pay(res["payToken"])
        self.assertEqual(self.sent[-1]["pg_amount"], "14380")                  # сумма — итог черновика
        self.assertIn("pay=ok&n=" + res["payToken"], self.sent[-1]["pg_success_url"])
        self.assertEqual(freedom.my_drafts("cid-1"), [])                       # оплачен — больше не «ожидает»
        freedom.after_paid(pg)
        body = self.posted[-1]
        self.assertEqual(body["state"]["meta"]["href"], "https://x/state/s-ok")  # сразу «Подтвержден»
        self.assertIn("Оплачено картой Freedom Pay, платёж №9001 — ТЕСТ", body["description"])
        self.assertIn(oh.CONSENT_TXT, body["description"])
        self.assertEqual(sum(p["price"] * p["quantity"] for p in body["positions"]) // 100, 14380)
        self.assertEqual(self.c.post("/pay/card/status", json={"token": res["payToken"]}).get_json()["number"], "1700")
        self.assertEqual(freedom.by_pg_order(pg)["number"], "1700")
        freedom.after_paid(pg)                                                 # повтор уведомления — второго заказа нет
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(json.loads(self.c.post("/pay/card/init", json={"token": res["payToken"]}).data)["error"], "Заказ уже оплачен")

    def test_short_stock_alert_and_retry(self):
        res, st = self.checkout()
        pg = self.pay(res["payToken"])
        self.stock = 1                                                         # пока платили — товар разобрали
        freedom.after_paid(pg)
        self.assertEqual(self.posted, [])
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("заказ не создан", self.alerts[0])
        self.assertEqual(self.c.post("/pay/card/status", json={"token": res["payToken"]}).get_json()["status"], "failed")
        freedom.retry_drafts()                                                 # всё ещё нет — тревога не повторяется
        self.assertEqual(len(self.alerts), 1)
        self.stock = 5
        freedom.retry_drafts()
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(freedom.by_pg_order(pg)["number"], "1700")
        self.assertEqual(freedom.get_draft(res["draft"])["status"], "ordered")

    def test_off_and_expired(self):
        modules.update("cardpay", on=False)
        res, st = self.checkout()                                              # оплата картой выключена — заказ сразу, как раньше
        self.assertEqual((st, res["number"]), (200, "1700"))
        modules.update("cardpay", on=True)
        res, st = self.checkout(payFirst=None)                                 # старая страница без payFirst — тоже как раньше
        self.assertIn("number", res)
        res, st = self.checkout()
        with freedom.db() as d:
            d.run("UPDATE card_draft SET created=%s", (time.time() - 2 * 86400,))
        self.assertEqual(freedom.my_drafts("cid-1"), [])
        self.assertIn("истекло", json.loads(self.c.post("/pay/card/init", json={"token": res["payToken"]}).data)["error"])
        self.assertEqual(self.c.post("/pay/card/status", json={"token": "D1_bad"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
