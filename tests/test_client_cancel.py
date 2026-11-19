"""Отмена оплаченного заказа покупателем (client_cancel.py): кнопка только пока не отгружен; отмена → «Отменен», резерв снят,
комментарий, Telegram владельцу; отгружен / курьер / ship_op — «оформите возврат»; чужой заказ — не найден; повтор — нельзя;
«Деньги возвращены» → один исходящий платёж со счёта Kaspi (статья «Возврат»), отметка у счёта Kaspi; покупатель видит статусы."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401  (общая настройка: SQLite, шрифты)
import admin  # noqa: E402
import apipay  # noqa: E402
import app  # noqa: E402
import client_cancel as cc  # noqa: E402
import courier  # noqa: E402
import freedom  # noqa: E402
import order_hook as oh  # noqa: E402
import shipments  # noqa: E402

API = oh.API
STATES = [{"name": n, "meta": {"href": f"https://x/state/{k}"}} for n, k in (("Новый", "s-new"), ("Подтвержден", "s-ok"), ("Отгружен", "s-sh"), ("Отменен", "s-off"))]


class ClientCancel(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        with cc.db() as d:
            d.run("DELETE FROM client_cancel")
        with apipay.db() as d:
            d.run("DELETE FROM kaspi_pay")
        oh._cache.clear()
        self.order = {"id": "o1", "name": "2200", "agent": {"name": "Айгерим", "meta": {"href": f"{API}/entity/counterparty/c1"}}, "sum": 669000, "payedSum": 669000,
                      "moment": "2026-11-19 10:00:00.000", "state": {"name": "Подтвержден", "meta": STATES[1]["meta"]}, "description": "Отправка: СДЭК",
                      "positions": {"rows": [{"quantity": 1, "price": 669000, "assortment": {"id": "p1", "name": "Крем", "meta": {"type": "product"}}}]}}
        self.puts, self.pouts, self.tgs, self.tracks, self.op, self.fail_link = [], [], [], {}, None, False

        def ms(method, path, params=None, json=None, **kw):
            if path == "/entity/customerorder/metadata":
                return {"states": STATES}
            if path == "/entity/customerorder" and method == "GET":
                f = (params or {}).get("filter", "")
                return {"rows": [self.order] if (f == "name=2200" or f.startswith("agent=")) else []}
            if path == "/entity/customerorder/o1/positions":
                return {"rows": [{"id": "pos1", "quantity": 1, "price": 669000, "reserve": 1, "assortment": {"id": "p1", "name": "Крем", "meta": {"type": "product", "href": "x/p1"}}}]}
            if path == "/entity/customerorder/o1" and method == "PUT":
                self.puts.append(json)
                self.order["state"] = {"name": "Отменен", "meta": STATES[3]["meta"]}
                return {}
            if path == "/entity/counterparty/c1":
                return {"id": "c1", "name": "Айгерим (Алматы)", "phone": "+77011234567", "actualAddress": "Алматы"}
            if path == "/entity/expenseitem":
                return {"rows": [{"meta": {"href": "x/expenseitem/ret"}, "name": "Возврат"}]}
            if path == "/entity/paymentout" and method == "GET":
                return {"rows": [p | {"name": "R1"} for p in self.pouts if p["externalCode"] == params["filter"].split("=", 1)[1]]}
            if path == "/entity/paymentout" and method == "POST":
                if self.fail_link and "operations" in json:
                    raise RuntimeError("МойСклад 400: нельзя связать")
                self.pouts.append(json)
                return {"name": "R1", "id": "po1"}
            if path.startswith("/entity/organization"):
                return {"rows": []}
            raise AssertionError((method, path))
        self.patch(oh, "ms", ms)
        self.patch(oh, "order_positions", lambda oid: ms("GET", f"/entity/customerorder/{oid}/positions")["rows"])
        self.patch(oh, "organization", lambda: "org1")
        self.patch(oh, "too_many", lambda *a, **k: False)
        self.patch(oh, "tg", lambda *a, **k: self.tgs.append(k.get("text", "")))
        self.patch(courier, "client_tracks", lambda cid, nums=None: dict(self.tracks))
        self.patch(shipments, "for_order", lambda oid: self.op)
        self.patch(freedom, "_ms_account", lambda m="card": {"meta": {"href": "x/acc/kaspi"}})
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + oh.make_token("c1")}
        self.ah = {"Authorization": "Bearer " + admin.make_admin_token()}

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(setattr, obj, name, old)

    def me(self):
        return next(o for o in self.c.get("/me", headers=self.h).get_json()["orders"] if o["number"] == "2200")

    def cancel(self, h=None):
        return self.c.post("/me/cancel", headers=h or self.h, json={"number": "2200"})

    def test_cancel_paid_not_shipped(self):
        self.assertTrue(self.me()["canCancel"])
        r = self.cancel()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["refund"], {"sum": 6690, "done": False})
        put = self.puts[0]
        self.assertEqual(put["state"]["meta"]["href"], "https://x/state/s-off")
        self.assertEqual(put["positions"][0]["reserve"], 0)
        self.assertIn("Отменён покупателем на сайте", put["description"])
        self.assertIn("ждёт возврата 6 690 ₸", put["description"].replace("\xa0", " "))
        self.assertIn("верните", self.tgs[0])
        self.assertIn("2200", self.tgs[0])
        o = self.me()
        self.assertEqual(o["refund"], {"sum": 6690, "done": False})
        self.assertNotIn("canCancel", o)
        self.assertEqual(self.cancel().status_code, 409)              # повтор — нельзя
        self.assertEqual(len(self.puts), 1)

    def test_shipped_branches(self):
        cases = [("demand", lambda: self.order.update(demands=[{"meta": {"href": "x/demand/d1"}}])),
                 ("state", lambda: self.order.update(state={"name": "Отгружен", "meta": STATES[2]["meta"]})),
                 ("courier", lambda: self.tracks.update({"2200": {"step": 0}})),
                 ("ship_op", lambda: setattr(self, "op", {"status": "sent"}))]
        for name, setup in cases:
            with self.subTest(name):
                self.order.pop("demands", None)
                self.order["state"] = {"name": "Подтвержден", "meta": STATES[1]["meta"]}
                self.tracks.clear()
                self.op = None
                setup()
                self.assertFalse(self.me().get("canCancel"))
                r = self.cancel()
                self.assertEqual(r.status_code, 409)
                self.assertTrue(r.get_json()["shipped"])
                self.assertEqual(r.get_json()["error"], cc.SHIPPED)
        self.assertEqual(self.puts, [])

    def test_unpaid_and_foreign(self):
        self.order["payedSum"] = 0
        self.assertFalse(self.me().get("canCancel"))
        self.assertEqual(self.cancel().status_code, 409)
        self.order["payedSum"] = 669000
        other = {"Authorization": "Bearer " + oh.make_token("c2")}
        self.patch(oh, "live_cid", lambda c: c)
        self.assertEqual(self.cancel(other).status_code, 404)
        self.assertEqual(self.puts, [])
        self.assertEqual(self.c.post("/me/cancel", json={"number": "2200"}).status_code, 401)

    def test_refund_done(self):
        with apipay.db() as d:
            d.run(f"INSERT INTO kaspi_pay ({apipay.COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (777, "2200", "o1", "c1", 6690, "87011234567", "paid", 1, 2, 0, "00042", "", "", 0))
        self.cancel()
        self.patch(admin, "_card_pay", lambda n: [])
        self.patch(admin, "_kaspi_pay", lambda n: [])
        d = self.c.get("/admin/api/orders/2200", headers=self.ah).get_json()
        self.assertEqual((d["clientCancel"]["amount"], d["clientCancel"]["done"]), (6690, False))   # окно заказа: «Ждёт возврата»
        r = self.c.post("/admin/api/orders/2200/refunded", headers=self.ah)
        self.assertEqual(r.status_code, 200, r.get_json())
        p = self.pouts[0]
        self.assertEqual(p["sum"], 669000)
        self.assertEqual(p["externalCode"], "refund-2200")
        self.assertEqual(p["expenseItem"]["meta"]["href"], "x/expenseitem/ret")
        self.assertEqual(p["organizationAccount"]["meta"]["href"], "x/acc/kaspi")
        self.assertEqual(p["operations"][0]["linkedSum"], 669000)
        self.assertEqual(apipay.by_invoice(777)["status"], "refund")
        c = cc.get("2200")
        self.assertTrue(c["done"])
        self.assertEqual(c["msPayment"], "R1")
        self.assertEqual(self.me()["refund"], {"sum": 6690, "done": True})
        self.c.post("/admin/api/orders/2200/refunded", headers=self.ah)   # повтор — второго платежа нет
        self.assertEqual(len(self.pouts), 1)

    def test_refund_link_fallback_and_guards(self):
        self.assertEqual(self.c.post("/admin/api/orders/2200/refunded", headers=self.ah).status_code, 400)   # не отменялся
        self.assertEqual(self.c.post("/admin/api/orders/2200/refunded").status_code, 401)
        self.cancel()
        self.fail_link = True
        self.assertEqual(self.c.post("/admin/api/orders/2200/refunded", headers=self.ah).status_code, 200)
        self.assertNotIn("operations", self.pouts[0])
        self.assertEqual(cc.get("2200")["msPayment"], "R1")



class KaspiRefund(ClientCancel):
    """«Вернуть через Kaspi»: POST /invoices/{id}/refund в ApiPay (заглушка), итог опросом GET /invoices/{id}/refunds."""
    test_cancel_paid_not_shipped = test_shipped_branches = test_unpaid_and_foreign = None     # их проверяет ClientCancel
    test_refund_done = test_refund_link_fallback_and_guards = None
    def setUp(self):
        super().setUp()
        os.environ["APIPAY_API_KEY"] = "k"
        self.addCleanup(lambda: os.environ.pop("APIPAY_API_KEY", None))
        with apipay.db() as d:
            d.run(f"INSERT INTO kaspi_pay ({apipay.COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (777, "2200", "o1", "c1", 6690, "87011234567", "paid", 1, 2, 0, "00042", "", "", 0))
        self.api, self.ref, self.err = [], [], None

        def call(method, path, **kw):
            self.api.append((method, path, kw.get("json")))
            if method == "POST" and path == "/invoices/777/refund":
                if self.err:
                    raise apipay.ApiError(400, *self.err)
                key = kw["json"]["idempotency_key"]
                old = next((r for r in self.ref if r["idempotency_key"] == key), None)
                if old:
                    return {"refund": old, "idempotent_replay": True}
                r = {"id": 50 + len(self.ref), "status": "pending", "idempotency_key": key, "operation_state": "processing"}
                self.ref.append(r)
                return {"refund": dict(r)}
            if method == "GET" and path == "/invoices/777/refunds":
                return {"refunds": [dict(r) for r in self.ref]}
            raise AssertionError((method, path))
        self.patch(apipay, "call", call)
        self.cancel()

    def kr(self):
        return self.c.post("/admin/api/orders/2200/kaspi-refund", headers=self.ah)

    def posts(self):
        return [a for a in self.api if a[0] == "POST"]

    def test_success_after_poll(self):
        r = self.kr()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["cancel"]["kaspiRefund"]["status"], "processing")
        self.assertFalse(cc.get("2200")["done"])                       # заявка — ещё не «деньги вернулись»
        self.assertEqual(self.posts()[0][2]["idempotency_key"], "amura-refund-2200-1")
        self.assertEqual(self.kr().status_code, 200)                    # повтор, пока идёт, — второго возврата нет
        self.assertEqual(len(self.posts()), 1)
        self.ref[0]["operation_state"], self.ref[0]["status"] = "completed", "completed"
        apipay.sweep()                                                  # фоновая сверка раз в минуту
        c = cc.get("2200")
        self.assertTrue(c["done"])
        self.assertEqual(c["kaspiRefund"]["status"], "completed")
        self.assertEqual(len(self.pouts), 1)                            # исходящий платёж, как «Деньги возвращены»
        self.assertEqual(apipay.by_invoice(777)["status"], "refund")
        self.assertEqual(self.me()["refund"], {"sum": 6690, "done": True})
        r = self.kr()
        self.assertEqual(r.status_code, 400)                            # уже вернули — второго возврата нет
        self.assertEqual(len(self.posts()), 1)

    def test_api_error_marks_nothing(self):
        self.err = ("tariff_inactive", "Tariff inactive")
        r = self.kr()
        self.assertEqual(r.status_code, 400)
        self.assertIn("тариф ApiPay", r.get_json()["error"])
        c = cc.get("2200")
        self.assertFalse(c["done"])
        self.assertEqual(self.pouts, [])
        self.assertEqual(apipay.by_invoice(777)["status"], "paid")
        self.err = None                                                 # причину устранили — новая попытка, новый ключ
        self.assertEqual(self.kr().status_code, 200)
        self.assertEqual(self.posts()[-1][2]["idempotency_key"], "amura-refund-2200-2")

    def test_failed_in_kaspi_then_retry(self):
        self.kr()
        self.ref[0].update(status="failed", operation_state="failed", error_code="refund_insufficient_funds")
        c = cc.poll("2200")
        self.assertEqual(c["kaspiRefund"]["status"], "failed")
        self.assertIn("не хватает денег", c["kaspiRefund"]["error"])
        self.assertFalse(c["done"])
        self.assertEqual(self.pouts, [])
        self.kr()
        self.assertEqual([p[2]["idempotency_key"] for p in self.posts()], ["amura-refund-2200-1", "amura-refund-2200-2"])

    def test_uncertain_blocks_retry(self):
        self.kr()
        self.ref[0].update(status="processing", operation_state="uncertain", error_code="refund_execution_uncertain")
        c = cc.poll("2200")
        self.assertEqual(c["kaspiRefund"]["status"], "uncertain")
        self.kr()
        self.assertEqual(len(self.posts()), 1)                          # деньги могли уйти — второй возврат не создаём
        self.assertFalse(cc.get("2200")["done"])

    def test_owner_only(self):
        self.assertEqual(self.c.post("/admin/api/orders/2200/kaspi-refund").status_code, 401)
        self.assertEqual(self.posts(), [])


if __name__ == "__main__":
    unittest.main()
