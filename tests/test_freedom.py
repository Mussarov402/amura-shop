"""Оплата картой Freedom Pay: подпись, создание платежа (сумма из МойСклад, только карта, тестовый режим),
Result URL — неверная подпись, повтор уведомления, сумма не совпала; платёж в МойСклад без дублей; выключено по умолчанию."""
import hashlib
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
ORDER = {"id": "o-uuid", "name": "1777", "sum": 1250000, "payedSum": 0, "description": oh.RETAIL_MARK + "\nАйгерим",
         "agent": {"meta": {"href": "https://x/entity/counterparty/agent-1"}}}


class Resp:
    def __init__(self, text):
        self.text = text


class Freedom(unittest.TestCase):
    def setUp(self):
        os.environ.update(FREEDOM_MERCHANT_ID="555", FREEDOM_SECRET_KEY=KEY, FREEDOM_TEST="1")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FREEDOM_MERCHANT_ID", "FREEDOM_SECRET_KEY", "FREEDOM_TEST")])
        with freedom.db() as d:
            d.run("DELETE FROM card_pay")
        modules.update("cardpay", on=True)
        self.addCleanup(lambda: modules.update("cardpay", on=False))
        self.sent, self.ms_posts, self.done, self.puts = [], [], [], []
        self.state = "s-new"
        oh._cache.pop("ms_order_states", None)
        self.patch(freedom.requests, "post", lambda url, data=None, timeout=None: self.sent.append((url, dict(data))) or Resp(
            "<?xml version='1.0'?><response><pg_status>ok</pg_status><pg_payment_id>9001</pg_payment_id>"
            "<pg_redirect_url>https://pay.example/9001</pg_redirect_url></response>"))

        def ms(method, path, **kw):
            if path == "/entity/customerorder":
                return {"rows": [dict(ORDER)]}
            if path == "/entity/paymentin" and method == "GET":
                return {"rows": [{"id": "pi", "name": "77"}] if self.ms_posts else []}
            if path == "/entity/paymentin" and method == "POST":
                self.ms_posts.append(kw["json"])
                return {"id": "pi", "name": "77"}
            if path.endswith("/accounts"):
                return {"rows": []}
            if path == "/entity/customerorder/metadata":
                return {"states": [{"name": n, "meta": {"href": "https://x/state/" + i}} for n, i in (("Новый", "s-new"), ("Подтвержден", "s-ok"), ("Собран", "s-built"))]}
            if path == "/entity/customerorder/o-uuid" and method == "GET":
                return dict(ORDER, state={"meta": {"href": "https://x/state/" + self.state}})
            if path == "/entity/customerorder/o-uuid" and method == "PUT":
                self.puts.append(kw["json"])
                return {}
            raise AssertionError(path)
        self.patch(oh, "ms", ms)
        self.patch(oh, "organization", lambda: "org-1")
        self.patch(oh, "too_many", lambda *a, **k: False)
        self.patch(oh, "notify_bg", lambda fn: self.done.append(fn))
        self.patch(oh, "tg", lambda *a, **k: None)
        self.patch(oh, "alert", lambda *a, **k: None)
        app = Flask(__name__)
        app.register_blueprint(freedom.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def notify(self, **kw):
        p = {"pg_merchant_id": "555", "pg_order_id": "1777-1", "pg_payment_id": "9001", "pg_amount": "12500", "pg_currency": "KZT",
             "pg_result": "1", "pg_can_reject": "1", "pg_salt": "abc"} | kw
        p["pg_sig"] = freedom.sign("result", p, KEY)
        return p

    def test_signature(self):
        self.assertEqual(freedom.sign("x", {"b": "2", "a": "1", "pg_sig": "zzz"}, "k"), hashlib.md5(b"x;1;2;k").hexdigest())
        p = self.notify()
        self.assertTrue(freedom.check("result", p, KEY))
        p["pg_amount"] = "1"
        self.assertFalse(freedom.check("result", p, KEY))

    def test_init_amount_from_ms_card_only_testing(self):
        r = json.loads(self.c.post("/pay/card/init", json={"token": "1777_" + oh.sign("1777")}).data)
        self.assertEqual(r["url"], "https://pay.example/9001")
        url, p = self.sent[-1]
        self.assertTrue(url.endswith("/init_payment.php"))
        self.assertEqual((p["pg_amount"], p["pg_payment_method"], p["pg_testing_mode"], p["pg_order_id"]), ("12500", "bankcard", "1", "1777-1"))
        self.assertTrue(freedom.check("init_payment.php", p, KEY))
        self.assertEqual(self.c.post("/pay/card/init", json={"token": "1777_bad"}).status_code, 404)    # чужой заказ без подписи

    def test_result_paid_once(self):
        self.c.post("/pay/card/init", json={"token": "1777_" + oh.sign("1777")})
        r = self.c.post("/pay/card/result", data=self.notify())
        self.assertIn(b"<pg_status>ok</pg_status>", r.data)
        self.assertEqual(freedom.by_pg_order("1777-1")["status"], "paid")
        self.assertEqual(len(self.done), 1)
        r = self.c.post("/pay/card/result", data=self.notify(pg_salt="other"))                      # повтор уведомления
        self.assertIn(b"<pg_status>ok</pg_status>", r.data)
        self.assertEqual(len(self.done), 1)                                                          # второй раз не учитываем
        self.assertEqual(freedom.for_order("1777")[0]["paymentId"], "9001")
        self.assertEqual(freedom.paid_numbers(["1777", "1778"]), {"1777"})

    def test_bad_signature_and_amount(self):
        self.c.post("/pay/card/init", json={"token": "1777_" + oh.sign("1777")})
        p = self.notify()
        p["pg_sig"] = "0" * 32
        self.assertIn(b"<pg_status>rejected</pg_status>", self.c.post("/pay/card/result", data=p).data)
        self.assertEqual(freedom.by_pg_order("1777-1")["status"], "new")
        r = self.c.post("/pay/card/result", data=self.notify(pg_amount="100"))
        self.assertIn(b"<pg_status>rejected</pg_status>", r.data)
        self.assertEqual(freedom.by_pg_order("1777-1")["status"], "mismatch")
        self.assertEqual(self.done, [])

    def test_ms_payment_once_and_not_in_test(self):
        self.c.post("/pay/card/init", json={"token": "1777_" + oh.sign("1777")})
        self.c.post("/pay/card/result", data=self.notify())
        self.done[0]()                                                       # тестовый платёж — в МойСклад не пишем
        self.assertEqual(self.ms_posts, [])
        with freedom.db() as d:
            d.run("UPDATE card_pay SET test=0")
        freedom.post_ms("1777-1")
        freedom.post_ms("1777-1")
        self.assertEqual(len(self.ms_posts), 1)
        b = self.ms_posts[0]
        self.assertEqual((b["externalCode"], b["sum"], b["operations"][0]["meta"]["type"]), ("freedom-9001", 1250000, "customerorder"))

    def test_paid_goes_to_assembly(self):
        r = json.loads(self.c.post("/pay/card/init", json={"token": "1777_" + oh.sign("1777")}).data)
        self.assertIn("pay=ok&n=1777", self.sent[-1][1]["pg_success_url"])
        self.c.post("/pay/card/result", data=self.notify())
        self.done[0]()
        self.assertEqual(self.puts[-1]["state"]["meta"]["href"], "https://x/state/s-ok")             # оплачен → «Подтвержден» (к сборке)
        self.assertIn("ТЕСТ", self.puts[-1]["description"])                                       # тестовый платёж — пометка в комментарии
        self.puts.clear()
        self.state = "s-built"                                                                     # уже собран — назад не переводим
        freedom.to_assembly(freedom.by_pg_order("1777-1"))
        self.assertEqual(self.puts, [])

    def test_off_by_default_and_without_keys(self):
        modules.update("cardpay", on=False)
        self.assertEqual(self.c.post("/pay/card/init", json={"token": "1777_" + oh.sign("1777")}).status_code, 404)
        modules.update("cardpay", on=True)
        os.environ.pop("FREEDOM_SECRET_KEY")
        self.assertFalse(freedom.available())
        self.assertIn(b"rejected", self.c.post("/pay/card/result", data={"pg_order_id": "1"}).data)


if __name__ == "__main__":
    unittest.main()
