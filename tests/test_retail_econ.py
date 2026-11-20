"""Экономика розничных заказов (20.11.2026): пришло, эквайринг Kaspi, доставка Яндекс (оценка → по заявке → факт), упаковка, налог ИП,
доля ApiPay, себестоимость, прибыль и маржа; сводка за период без отменённых; блок в описании МС заменяется, а не дописывается."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask  # noqa: E402

import admin  # noqa: E402
import inbox  # noqa: E402
import order_hook as oh  # noqa: E402
import retail_econ as re_  # noqa: E402

DLV = "dlv-service-id"


def pos(aid, qty, price, kind="product"):
    return {"quantity": qty, "price": price * 100, "discount": 0,
            "assortment": {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/{kind}/{aid}", "type": kind}}}


def order(num="2184", paid=True, ship="Курьер по Алматы (Яндекс) — Абая 10, 21.11 После обеда 14:00–17:30", state="Подтвержден",
          card=False, moment="2026-11-19 10:00:00"):
    lines = [pos("p1", 2, 6000), pos("p2", 1, 3000), pos(DLV, 1, 1500, "service")]
    s = 2 * 6000 + 3000 + 1500
    desc = f"{oh.RETAIL_MARK}\nАружан, WhatsApp +77010000000\nГород: Алматы\nОтправка: {ship}" + (f"\n{oh.PAY_CARD_TXT}" if card else "")
    return {"id": "o-" + num, "name": num, "moment": moment, "sum": s * 100, "payedSum": s * 100 if paid else 0,
            "description": desc, "state": {"name": state}, "positions": {"meta": {"size": 3}, "rows": lines}}


COST = {"p1": (3000, "приёмка №1"), "p2": (1500, "карточка МС")}
CONF = {"acq": 0.95, "acq_card": 3.5, "tax": 2.0, "apipay": 10000, "pack": 200}


class Calc(unittest.TestCase):
    def setUp(self):
        old = oh.delivery_service_id
        oh.delivery_service_id = lambda: DLV
        self.addCleanup(lambda: setattr(oh, "delivery_service_id", old))

    def calc(self, o, claim=None):
        return re_.calc(o, dict(CONF), claim=claim, cost=lambda pid: COST.get(pid, (None, "")), per_month=20)

    def test_yandex_fact(self):
        e = self.calc(order(), claim={"price": 1100.0, "status": "delivered"})
        self.assertEqual((e["goods"], e["dlvIn"], e["got"]), (15000, 1500, 16500))
        self.assertEqual(e["acq"], round(16500 * 0.0095))                    # 157
        self.assertEqual((e["dlvOut"], e["dlvNote"], e["dlvEst"]), (1100, "факт Яндекса", False))
        self.assertEqual((e["pack"], e["tax"], e["apipay"], e["cost"]), (200, 330, 500, 7500))
        self.assertEqual(e["profit"], 16500 - (157 + 1100 + 200 + 330 + 500 + 7500))
        self.assertEqual((e["dlvDiff"], e["est"]), (400, False))
        t = re_.text(e)
        self.assertIn("Пришло: товары 15 000 + доставка от покупателя 1 500 = 16 500 (оплачено)", t)
        self.assertIn("Доставка: −1 100 (факт Яндекса)", t)
        self.assertIn("Доставка: покупатель заплатил 1 500, Яндекс взял 1 100 → +400", t)
        self.assertIn("Kaspi Pay эквайринг: −157 (0,95 %)", t)
        self.assertIn(f"Прибыль: {e['profit']:,}".replace(",", " "), t)

    def test_estimates(self):
        e = self.calc(order(), claim={"price": 1300.0, "status": "ready_for_approval"})
        self.assertEqual((e["dlvOut"], e["dlvEst"], e["est"]), (1300, True, True))
        self.assertIn("курьер не вызван", e["dlvNote"])
        self.assertEqual(self.calc(order(), claim={"price": 1300.0, "status": "pickuped"})["dlvNote"], "по заявке Яндекса")
        e = self.calc(order(), claim=None)
        self.assertEqual((e["dlvOut"], e["dlvEst"]), (1500, True))                # заявки нет — как заплатил покупатель
        e = self.calc(order(ship="Пункт выдачи СДЭК — Абая 1 (код A1)"), claim=None)
        self.assertIn("СДЭК", e["dlvNote"])
        e = self.calc(order(paid=False, card=True), claim={"price": 1100.0, "status": "delivered"})
        self.assertEqual((e["paid"], e["est"], e["acq"]), (False, True, round(16500 * 0.035)))
        self.assertIn("ещё не оплачено", re_.text(e))
        self.assertIn("Эквайринг картой", re_.text(e))

    def test_no_cost_marked(self):
        e = re_.calc(order(), dict(CONF), claim={"price": 1100.0, "status": "delivered"}, cost=lambda pid: (None, ""), per_month=1)
        self.assertEqual((e["cost"], e["noCost"], e["est"], e["apipay"]), (0, 2, True, 10000))

    def test_block_replaced(self):
        d1 = re_.with_block(order()["description"], "Экономика заказа, ₸:\nПрибыль: 1")
        d2 = re_.with_block(d1, "Экономика заказа, ₸:\nПрибыль: 2")
        self.assertEqual(d2.count("Экономика заказа"), 1)
        self.assertTrue(d2.startswith(oh.RETAIL_MARK) and d2.endswith("Прибыль: 2"))


class Api(unittest.TestCase):
    def setUp(self):
        for name, val in {"delivery_service_id": lambda: DLV}.items():
            old = getattr(oh, name)
            setattr(oh, name, val)
            self.addCleanup(lambda n=name, o=old: setattr(oh, n, o))
        self.rows = [order("2184"), order("2185", state="Отменен"), order("2186", ship="Самовывоз — склад")]

        def ms(method, path, params=None, **kw):
            if path == "/entity/customerorder":
                f = (params or {}).get("filter", "")
                if f.startswith("name="):
                    return {"rows": [r for r in self.rows if r["name"] == f[5:]]}
                return {"rows": self.rows}
            raise AssertionError(path)
        old = oh.ms
        oh.ms = ms
        self.addCleanup(lambda: setattr(oh, "ms", old))
        old_cost = re_._cost
        re_._cost = lambda pid: COST.get(pid, (None, ""))
        self.addCleanup(lambda: setattr(re_, "_cost", old_cost))
        old_claim = re_._claim
        re_._claim = lambda num: {"price": 1100.0, "status": "delivered"} if num == "2184" else None
        self.addCleanup(lambda: setattr(re_, "_claim", old_claim))
        for k in [k for k in oh._cache if str(k).startswith("recon_m:")]:
            oh._cache.pop(k)
        with inbox.db() as d:
            inbox.set_setting(d, re_.KEY, json.dumps({"pack": 200}))
        self.role = "owner"
        old_who = admin.who
        admin.who = lambda: {"role": self.role, "perms": ["orders"], "name": "x"}
        self.addCleanup(lambda: setattr(admin, "who", old_who))
        app = Flask(__name__)
        app.register_blueprint(re_.bp)
        self.c = app.test_client()

    def test_period_excludes_cancelled(self):
        j = self.c.get("/admin/api/retail/economy?p=from&from=2026-11-01&to=2026-11-20").get_json()
        self.assertTrue(j["ok"], j)
        self.assertEqual((j["orders"], j["cancelled"], j["cancelledSum"]), (2, 1, 16500))
        self.assertEqual(j["got"], 33000)
        self.assertEqual(j["dlvOut"], 1100)                                      # самовывоз — 0, 2184 — факт Яндекса
        self.assertEqual(j["avg"], 16500)
        self.assertEqual(len(j["items"]), 3)

    def test_order_and_access(self):
        j = self.c.get("/admin/api/orders/2184/economy").get_json()
        self.assertEqual((j["econ"]["dlvOut"], j["econ"]["perMonth"]), (1100, 2))  # ApiPay делится на 2 неотменённых заказа месяца
        self.assertIn("Экономика заказа, ₸", j["text"])
        self.role = "staff"
        self.assertEqual(self.c.get("/admin/api/orders/2184/economy").status_code, 403)   # прибыль и себестоимость — только владелец
        self.assertEqual(self.c.get("/admin/api/retail/economy").status_code, 403)

    def test_settings(self):
        r = self.c.put("/admin/api/retail/economy/settings", json={"tax": "3", "apipay": 12000})
        self.assertEqual((r.get_json()["conf"]["tax"], r.get_json()["conf"]["apipay"]), (3.0, 12000.0))
        self.assertEqual(self.c.put("/admin/api/retail/economy/settings", json={"tax": "80"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
