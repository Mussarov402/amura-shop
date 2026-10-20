"""Оплата Kaspi (ApiPay) на рознице: выключено по умолчанию; черновик → счёт по номеру (одной суммой, номер 8XXXXXXXXXX) →
опрос статуса → заказ «Подтвержден» один раз; песочница не пишет платёж в МойСклад; отменён / истёк могут стать оплаченными;
сумма не совпала — тревога; ушёл платить картой — счёт Kaspi отменяется. ApiPay здесь — заглушка, МойСклад — заглушка."""
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
import apipay  # noqa: E402
import freedom  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402

ITEM = {"id": "p1", "name": "Крем", "qty": 5, "opt": 5000, "mid": 0, "box": 0, "boxQty": 0, "rtl": 6690}


class Resp:
    def __init__(self, code, body):
        self.status_code, self.body = code, body

    def json(self):
        return self.body


class FakeApiPay:
    """Счета в памяти, как отвечает ApiPay: POST /invoices → processing, GET → текущий статус."""
    def __init__(self, sandbox=True):
        self.inv, self.sent, self.sandbox, self.next = {}, [], sandbox, 500

    def __call__(self, method, url, headers=None, timeout=None, json=None, **kw):
        path = url[len(apipay.API):]
        self.sent.append((method, path, json, dict(headers or {})))
        if method == "POST" and path == "/invoices":
            self.next += 1
            self.inv[self.next] = {"id": self.next, "amount": f"{json['amount']}.00", "status": "processing", "is_sandbox": self.sandbox}
            return Resp(201, {"id": self.next, "amount": f"{json['amount']}.00", "status": "processing"})
        if method == "GET" and path.startswith("/invoices/"):
            return Resp(200, self.inv[int(path.rsplit("/", 1)[1])])
        if method == "POST" and path.endswith("/cancel"):
            self.inv[int(path.split("/")[2])]["status"] = "cancelled"
            return Resp(200, {"success": True})
        raise AssertionError((method, path))

    def last(self):
        return self.inv[self.next]


class Kaspi(unittest.TestCase):
    def setUp(self):
        os.environ["APIPAY_API_KEY"] = "test-key"
        self.addCleanup(lambda: os.environ.pop("APIPAY_API_KEY", None))
        with apipay.db() as d:
            d.run("DELETE FROM kaspi_pay")
        with freedom.db() as d:
            d.run("DELETE FROM card_pay")
            d.run("DELETE FROM card_draft")
        apipay._seen.clear()
        apipay._retry_at[0] = 0
        modules.update("kaspipay", on=True)
        self.addCleanup(lambda: modules.update("kaspipay", on=False))
        self.api = FakeApiPay()
        self.patch(apipay.requests, "request", self.api)
        self.posted, self.alerts, self.payments, self.tgs, self.stock = [], [], [], [], 5
        oh._cache.pop("ms_order_states", None)

        def ms(method, path, **kw):
            if path == "/entity/customerorder/metadata":
                return {"states": [{"name": "Новый", "meta": {"href": "https://x/state/s-new"}}, {"name": "Подтвержден", "meta": {"href": "https://x/state/s-ok"}}]}
            if path == "/entity/customerorder/o1" and method == "GET":
                return {"id": "o1", "state": {"meta": {"href": "https://x/state/s-ok"}}, "description": ""}
            if path == "/entity/paymentin" and method == "GET":
                return {"rows": []}
            if path == "/entity/paymentin" and method == "POST":
                self.payments.append(kw["json"])
                return {"name": "00042", "id": "pin-1"}
            raise AssertionError((method, path))
        for name, val in {"catalog": lambda: {"p1": dict(ITEM, qty=self.stock)}, "too_many": lambda *a, **k: False, "ms": ms,
                          "organization": lambda: "org-1", "store_id": lambda: "store-1", "delivery_service_id": lambda: "dlv-1",
                          "notify_bg": lambda fn: None, "mark_retail": lambda *a: None, "tg": lambda *a, **k: self.tgs.append(k.get("text", "")),
                          "alert": lambda kind, text: kind == "kaspipay" and self.alerts.append(text),
                          "_post_order": lambda body: self.posted.append(body) or {"name": "1700", "id": "o1"}}.items():
            self.patch(oh, name, val)
        self.patch(freedom, "_ms_account", lambda method="card": {"meta": {"href": f"https://x/account/{method}"}})
        import checkout
        self.patch(checkout, "order_delivery", lambda d, ship, city, goods: (1000, "Курьер по Алматы (Яндекс) — Абая 1", None))
        app = Flask(__name__)
        app.register_blueprint(apipay.bp)
        app.register_blueprint(freedom.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def checkout(self, **kw):
        d = {"name": "Айгерим", "phone": "77012345678", "city": "Алматы", "shipping": "courier", "address": "Абая 1", "mode": "retail",
             "items": [{"id": "p1", "qty": 2}], "pay": "kaspi", "payFirst": True, "consent": True} | kw
        return oh.order_core(d, "k" + str(time.time()), "1.1.1.1", "cid-1")

    def init(self, token):
        return self.c.post("/pay/kaspi/init", json={"token": token}).get_json()

    def status(self, token):
        apipay._seen.clear()                                                   # в тесте не ждём 3 с между опросами
        return self.c.post("/pay/kaspi/status", json={"token": token}).get_json()

    def test_off_by_default_and_phone(self):
        self.assertFalse(modules.REGISTRY["kaspipay"].get("default"))
        modules.update("kaspipay", on=False)
        self.assertFalse(apipay.available())
        self.assertEqual(self.c.post("/pay/kaspi/init", json={"token": "D1_x"}).status_code, 404)
        modules.update("kaspipay", on=True)
        os.environ.pop("APIPAY_API_KEY")
        self.assertFalse(apipay.available())                                    # без ключа на Render — варианта нет
        for raw, want in (("+7 701 234 56 78", "87012345678"), ("87012345678", "87012345678"), ("7012345678", "87012345678"),
                          ("+7 727 123 45 67", ""), ("12345", "")):
            self.assertEqual(apipay.phone8(raw), want, raw)

    def test_paid_creates_order_once(self):
        res, st = self.checkout()
        self.assertEqual((st, res["total"]), (200, 14380))
        self.assertEqual(self.posted, [])                                      # до оплаты в МойСклад ничего
        j = self.init(res["payToken"])
        self.assertEqual((j["ok"], j["status"], j["phone"]), (True, "processing", "+7 701 234 56 78"))
        m, path, body, hdr = self.api.sent[0]
        self.assertEqual((m, path), ("POST", "/invoices"))
        self.assertEqual(body["phone_number"], "87012345678")
        self.assertEqual(body["amount"], 14380)                                # одной суммой, целые тенге
        self.assertNotIn("cart_items", body)
        self.assertLessEqual(len(body["description"]), 60)
        self.assertEqual(body["external_order_id"], f"D{res['draft']}")
        self.assertEqual(hdr["X-API-Key"], "test-key")
        self.assertEqual(self.init(res["payToken"])["status"], "processing")   # повтор — тот же счёт, второго нет
        self.assertEqual(sum(1 for s in self.api.sent if s[1] == "/invoices"), 1)
        self.api.last()["status"] = "pending"
        self.assertEqual(self.status(res["payToken"])["status"], "pending")
        self.api.last()["status"] = "paid"
        j = self.status(res["payToken"])
        self.assertEqual((j["status"], j["draft"]), ("paid", "paid"))
        apipay.after_paid(self.api.next)
        body = self.posted[-1]
        self.assertEqual(body["state"]["meta"]["href"], "https://x/state/s-ok")  # сразу «Подтвержден»
        self.assertIn(f"Оплачено Kaspi (ApiPay), счёт №{self.api.next} — ТЕСТ", body["description"])
        self.assertEqual(self.payments, [])                                    # песочница — платёж в МойСклад не пишем
        self.assertIn("Оплачен заказ № 1700 через Kaspi", self.tgs[-1])
        self.assertEqual(self.status(res["payToken"])["number"], "1700")
        self.assertEqual(self.c.post("/pay/card/status", json={"token": res["payToken"]}).get_json()["number"], "1700")   # экран «Заказ оплачен»
        self.assertEqual(apipay.for_order("1700")[0]["status"], "paid")       # система AMURA: «Оплачено Kaspi»
        apipay.after_paid(self.api.next)
        apipay.sweep()
        self.assertEqual(len(self.posted), 1)                                  # второго заказа нет
        self.assertEqual(self.init(res["payToken"])["error"], "Заказ уже оплачен")

    def test_live_payment_goes_to_kaspi_account(self):
        self.api.sandbox = False
        res, _ = self.checkout()
        self.init(res["payToken"])
        self.api.last()["status"] = "paid"
        self.status(res["payToken"])
        apipay.after_paid(self.api.next)
        self.assertEqual(len(self.payments), 1)
        p = self.payments[0]
        self.assertEqual((p["sum"], p["externalCode"]), (1438000, f"apipay-{self.api.next}"))
        self.assertEqual(p["organizationAccount"]["meta"]["href"], "https://x/account/kaspi")
        self.assertIn("Оплачено Kaspi (ApiPay), счёт №", p["paymentPurpose"])
        self.assertEqual(p["operations"][0]["linkedSum"], 1438000)
        self.assertEqual(apipay.for_order("1700")[0]["msPayment"], "00042")

    def test_closed_then_paid_and_reissue(self):
        for st in ("cancelled", "expired", "error"):
            with self.subTest(st=st):
                with apipay.db() as d:
                    d.run("DELETE FROM kaspi_pay")
                self.posted.clear()
                res, _ = self.checkout()
                self.init(res["payToken"])
                self.api.last().update(status=st, error_message="Номер не в Kaspi" if st == "error" else None)
                j = self.status(res["payToken"])
                self.assertEqual(j["status"], st)
                if st == "error":
                    self.assertIn("Номер не в Kaspi", j["error"])
                self.assertEqual(self.posted, [])
                self.api.last()["status"] = "paid"                             # закрытый счёт стал оплаченным — фоновая сверка видит
                apipay.sweep()
                self.assertEqual(apipay.last_for(f"D{res['draft']}")["status"], "paid")
        res, _ = self.checkout()                                               # отменён — новый счёт по тому же черновику
        first = self.init(res["payToken"])
        self.api.last()["status"] = "cancelled"
        self.status(res["payToken"])
        n0 = self.api.next
        self.assertEqual(self.init(res["payToken"])["status"], "processing")
        self.assertEqual(self.api.next, n0 + 1)
        self.assertTrue(first["ok"])
        with apipay.db() as d:                                                 # закрыт больше суток назад — больше не спрашиваем
            d.run("UPDATE kaspi_pay SET status='cancelled', closed_at=%s", (time.time() - 2 * 86400,))
        before = len(self.api.sent)
        apipay.sweep()
        self.assertEqual(len(self.api.sent), before)

    def test_failed_order_retried_every_10_minutes(self):
        res, _ = self.checkout()
        self.init(res["payToken"])
        self.api.last()["status"] = "paid"
        self.stock = 1                                                         # пока платили — товар разобрали
        self.status(res["payToken"])
        apipay.after_paid(self.api.next)
        self.assertEqual(freedom.get_draft(res["draft"])["status"], "failed")
        self.assertEqual(len(self.alerts), 1)
        tries = lambda: freedom.get_draft(res["draft"])["tries"]
        apipay.sweep()                                                         # первая сверка — повтор
        self.assertEqual(tries(), 2)
        for _ in range(5):                                                     # следующие минуты — без повторов
            apipay.sweep()
        self.assertEqual(tries(), 2)
        apipay._retry_at[0] -= apipay.RETRY_GAP                                # прошло 10 минут
        self.stock = 5
        apipay.sweep()
        self.assertEqual(freedom.get_draft(res["draft"])["status"], "ordered")
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(len(self.alerts), 1)                                  # тревога была одна

    def test_amount_mismatch_and_bad_token(self):
        res, _ = self.checkout()
        self.init(res["payToken"])
        self.api.last().update(status="paid", amount="100.00")
        self.assertEqual(self.status(res["payToken"])["status"], "mismatch")
        self.assertEqual(self.posted, [])
        self.assertIn("сумма 100", self.alerts[-1])
        self.assertEqual(self.c.post("/pay/kaspi/status", json={"token": f"D{res['draft']}_bad"}).status_code, 404)
        self.assertEqual(self.c.post("/pay/kaspi/init", json={"token": "1700_" + oh.sign("1700")}).status_code, 404)   # только черновики

    def test_card_after_kaspi_cancels_invoice(self):
        os.environ.update(FREEDOM_MERCHANT_ID="555", FREEDOM_SECRET_KEY="k", FREEDOM_TEST="1")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FREEDOM_MERCHANT_ID", "FREEDOM_SECRET_KEY", "FREEDOM_TEST")])
        modules.update("cardpay", on=True)
        self.addCleanup(lambda: modules.update("cardpay", on=False))

        class R:
            text = ("<?xml version='1.0'?><response><pg_status>ok</pg_status><pg_payment_id>9001</pg_payment_id>"
                    "<pg_redirect_url>https://pay.example/9001</pg_redirect_url></response>")
        self.patch(freedom.requests, "post", lambda *a, **k: R())
        res, _ = self.checkout()
        self.init(res["payToken"])
        self.api.last()["status"] = "pending"
        self.assertTrue(self.c.post("/pay/card/init", json={"token": res["payToken"]}).get_json()["ok"])
        self.assertEqual(apipay.last_for(f"D{res['draft']}")["status"], "cancelled")

    def test_catalog_flag(self):
        self.patch(oh, "live", lambda max_age=None: {"updated": "x", "items": [dict(ITEM)]})
        self.addCleanup(lambda: [oh._cache.pop(k) for k in list(oh._cache) if isinstance(k, tuple) and k[0] == "catjson"])
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        c = app.test_client()
        rule = "/catalog"
        on = json.loads(c.get(rule + "?mode=retail").data)
        self.assertTrue(on.get("kaspiPay"))
        self.assertNotIn("kaspiPay", json.loads(c.get(rule).data))            # опт — без Kaspi
        modules.update("kaspipay", on=False)
        self.assertNotIn("kaspiPay", json.loads(c.get(rule + "?mode=retail").data))


if __name__ == "__main__":
    unittest.main()
