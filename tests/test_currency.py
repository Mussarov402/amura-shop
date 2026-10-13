"""Валюта документов и закупочных цен (доллар по курсу МойСклад): зеркало хранит суммы в тенге и исходную валюту,
оплаты, приёмки и возвраты по валютным документам уходят в МойСклад в своей валюте с курсом."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401
import test_payments as tp  # noqa: E402
import test_returns as tr  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402
import payments  # noqa: E402
import whops  # noqa: E402

USD = {"currency": {"meta": {"href": "https://x/entity/currency/usd"}}, "value": 472.9}
KZT = {"currency": {"meta": {"href": "https://x/entity/currency/kzt"}}}
COL = mirror._DCOLS.split(", ")


def set_currencies(usd=472.9):
    with mirror.db() as d:
        d.run("DELETE FROM ms_currency")
        d.run("INSERT INTO ms_currency (id, iso, name, rate, is_default) VALUES ('kzt', 'KZT', 'тенге', 1, 1)")
        d.run("INSERT INTO ms_currency (id, iso, name, rate, is_default) VALUES ('usd', 'USD', 'доллар', %s, 0)", (usd,))
    mirror._CUR["by_id"] = {}


class MirrorCurrencyTest(unittest.TestCase):
    def setUp(self):
        set_currencies()

    def test_usd_doc_in_tenge(self):
        """Приёмка №00291 в долларах: 2149.80 $ по 472.9 — у нас 1 016 640 ₸, валюта и исходная сумма сохранены."""
        row = {"id": "s291", "name": "00291", "moment": "2026-10-13 17:34:00", "sum": 214980, "payedSum": 100000, "rate": USD, "applicable": True}
        r = dict(zip(COL, mirror._doc_parser("supply")(row, 0)))
        self.assertEqual((r["cur"], r["rate"], r["sum_cur"]), ("USD", 472.9, 2149.8))
        self.assertAlmostEqual(r["sum"], 2149.8 * 472.9)
        self.assertAlmostEqual(r["paid"], 1000 * 472.9)
        ln = mirror._line("s291", {"id": "l1", "quantity": 98, "price": 950, "assortment": {"meta": {"href": "x/product/p1"}}}, 472.9)
        self.assertAlmostEqual(ln[4], 9.5 * 472.9)                    # цена позиции — в тенге
        self.assertEqual(mirror.money_label("USD", 2149.8, 472.9), "$2 149,80 по 472,9")

    def test_tenge_doc_unchanged(self):
        for rate in (None, KZT, {**KZT, "value": 1}):
            row = {"id": "d1", "name": "1", "sum": 500000, "applicable": True, **({"rate": rate} if rate else {})}
            r = dict(zip(COL, mirror._doc_parser("demand")(row, 0)))
            self.assertEqual((r["cur"], r["rate"], r["sum"]), ("", 1.0, 5000))
        self.assertEqual(mirror.money_label("", 5000), "")
        self.assertAlmostEqual(mirror._tenge({"sum": 214980, "rate": USD}), 2149.8 * 472.9)   # сверка: МойСклад в тенге тоже

    def test_buy_price_in_card_currency(self):
        """Закупка в долларах (Celimax 9.5 $): в тенге — по текущему курсу, при смене курса пересчитывается."""
        row = {"id": "pc", "name": "Celimax", "buyPrice": {"value": 950, "currency": {"meta": {"href": "https://x/entity/currency/usd"}}}}
        r = dict(zip(mirror._COLS.split(", "), mirror._parse("product", row, 0)))
        self.assertEqual((r["buy_price"], r["buy_raw"], r["buy_cur"]), (round(9.5 * 472.9), 9.5, "USD"))
        r = dict(zip(mirror._COLS.split(", "), mirror._parse("product", {"id": "pk", "buyPrice": {"value": 120000}}, 0)))
        self.assertEqual((r["buy_price"], r["buy_raw"], r["buy_cur"]), (1200, 1200, ""))
        with mirror.db() as d:
            d.run("DELETE FROM ms_product WHERE id='pc'")
            d.run("INSERT INTO ms_product (id, kind, name, buy_price, buy_raw, buy_cur, deleted, archived) VALUES ('pc', 'product', 'C', 4493, 9.5, 'USD', 0, 0)")
        old = oh.ms
        oh.ms = lambda m, p, **k: {"rows": [{"id": "kzt", "isoCode": "KZT", "rate": 1, "default": True},
                                           {"id": "usd", "isoCode": "USD", "rate": 500, "default": False}]}
        try:
            mirror.sync_currency()
        finally:
            oh.ms = old
        with mirror.db() as d:
            self.assertEqual(d.run("SELECT buy_price FROM ms_product WHERE id='pc'", one=True)[0], 4750)
        set_currencies()

    def test_light_pass_refetches_usd_lines(self):
        """Валютный документ, загруженный ещё без курса, получает позиции заново (теперь — в тенге)."""
        fake = tm.FakeMS()
        fake.docs["supply"] = [{"id": "su1", "name": "1", "moment": "2026-10-13 10:00:00", "updated": "2026-10-13 10:00:00.000", "sum": 1000,
                                "rate": USD, "applicable": True, "_all_pos": [{"id": "l1", "quantity": 1, "price": 1000,
                                                                               "assortment": {"meta": {"href": "x/product/p1"}}}]}]
        old = oh.ms
        oh.ms = fake
        try:
            with mirror.db() as d:
                d.run("DELETE FROM ms_doc WHERE id='su1'")
                d.run("DELETE FROM ms_doc_line WHERE doc_id='su1'")
                d.run("INSERT INTO ms_doc (id, type, number, updated, sum, deleted) VALUES ('su1', 'supply', '1', '2026-10-13 10:00:00.000', 10, 0)")
                d.run("INSERT INTO ms_doc_line (doc_id, pos_id, product_id, qty, price, discount, sum) VALUES ('su1', 'l1', 'p1', 1, 10, 0, 10)")
                d.run("DELETE FROM ms_sync WHERE entity='supply'")
                d.run("INSERT INTO ms_sync (entity, cursor, skip, full_from, full_done, rows) VALUES ('supply', '', 0, 1, 1, 0)")
            mirror.sync_entity("supply", pause=0)
            with mirror.db() as d:
                self.assertAlmostEqual(d.run("SELECT sum FROM ms_doc_line WHERE doc_id='su1'", one=True)[0], 4729)
                self.assertEqual(d.run("SELECT cur FROM ms_doc WHERE id='su1'", one=True)[0], "USD")
        finally:
            oh.ms = old


class PayCurrencyTest(unittest.TestCase):
    """Подготовка — как у тестов денег (подменённый МойСклад), без их собственных тестов."""
    def setUp(self):
        tp.PaymentsTest.setUp(self)
        set_currencies()
        with mirror.db() as d:
            d.run("INSERT INTO ms_doc (id, type, number, moment, agent_id, sum, applicable, deleted, cur, rate, sum_cur, paid)"
                  " VALUES ('su9', 'supply', '00291', '2026-10-13 10:00:00', 's1', %s, 1, 0, 'USD', 472.9, 2149.8, 0)", (2149.8 * 472.9,))

    def test_pay_usd_supply_in_usd(self):
        due = payments.doc_due("su9")
        self.assertEqual((due["cur"], due["dueCur"]), ("USD", 2149.8))
        with self.assertRaises(ValueError):                                    # больше долга в долларах — нельзя
            payments.create("out", "s1", 2200, account="acc1", expense="e1", doc_id="su9")
        op = payments.create("out", "s1", 1000, account="acc1", expense="e1", doc_id="su9")
        self.assertEqual((op["status"], op["cur"], op["orig"]), ("sent", "USD", "$1 000,00"))
        body = self.ms.docs["paymentout"][-1]
        self.assertEqual(body["sum"], 100000)                                  # центы, не тенге
        self.assertTrue(body["rate"]["currency"]["meta"]["href"].endswith("/entity/currency/usd"))
        self.assertEqual(body["rate"]["value"], 472.9)
        self.assertEqual(body["operations"][0]["linkedSum"], 100000)
        self.assertAlmostEqual(payments.doc_due("su9")["dueCur"], 1149.8)      # оплачено в тенге по курсу документа

    def test_tenge_payment_has_no_rate(self):
        payments.create("in", "k1", 500, account="acc1", doc_id="dm1")
        self.assertNotIn("rate", self.ms.docs["paymentin"][-1])


class WhCurrencyTest(unittest.TestCase):
    ret = tr.ReturnsTest.ret

    def setUp(self):
        tr.ReturnsTest.setUp(self)
        set_currencies()
        with mirror.db() as d:
            d.run("UPDATE ms_doc SET cur='USD', rate=472.9, sum_cur=95, sum=%s WHERE id='sp1'", (95 * 472.9,))
            d.run("UPDATE ms_doc_line SET price=%s, sum=%s WHERE doc_id='sp1'", (9.5 * 472.9, 95 * 472.9))

    def test_supply_in_usd(self):
        op = whops.create("supply", "sA", [{"id": "p1", "qty": 98, "price": 9.499}], agent_id="k1", cur="USD")
        self.assertEqual((op["status"], op["cur"], op["lines"][0]["price"]), ("sent", "USD", 9.5))
        doc = self.ms.docs["supply"][-1]
        self.assertEqual(doc["positions"][0]["price"], 950)                   # 9.50 $ в центах
        self.assertEqual(doc["rate"]["value"], 472.9)
        self.assertTrue(doc["rate"]["currency"]["meta"]["href"].endswith("/entity/currency/usd"))
        with self.assertRaises(ValueError):
            whops.create("loss", "sA", [{"id": "p1", "qty": 1}], cur="USD")   # списание — только в тенге

    def test_tenge_supply_has_no_rate(self):
        whops.create("supply", "sA", [{"id": "p1", "qty": 1, "price": 1200.4}], agent_id="k1", cur="KZT")
        doc = self.ms.docs["supply"][-1]
        self.assertNotIn("rate", doc)
        self.assertEqual(doc["positions"][0]["price"], 120000)

    def test_return_to_supplier_in_usd(self):
        dm = whops.demand_lines("sp1", "purchasereturn")
        self.assertEqual((dm["cur"], dm["lines"][0]["price"]), ("USD", 9.5))   # цена — в долларах документа
        op = self.ret(demand="sp1", kind="purchasereturn", lines=[{"id": "p1", "qty": 2}]).get_json()["op"]
        doc = self.ms.docs["purchasereturn"][0]
        self.assertEqual((op["cur"], doc["positions"][0]["price"], doc["rate"]["value"]), ("USD", 950, 472.9))


if __name__ == "__main__":
    unittest.main()


class CardCurrencyTest(unittest.TestCase):
    def setUp(self):
        import test_cards as tc
        tc.CardsTest.setUp(self)
        set_currencies()
        with mirror.db() as d:
            d.run("UPDATE ms_product SET buy_raw=9.5, buy_cur='USD', buy_price=4493 WHERE id='p1'")

    def _ms(self, cur_href):
        self.sent = []
        cur = {"id": "p1", "name": "Крем", "buyPrice": {"value": 950, "currency": {"meta": {"href": f"https://x/entity/currency/{cur_href}"}}}}

        def ms(method, path, params=None, json=None, **kw):
            if method == "GET" and path == "/entity/product/p1":
                return cur
            if method == "PUT" and path == "/entity/product/p1":
                self.sent.append(json)
                return {}
            return {"rows": []}
        oh.ms = ms

    def test_usd_buy_price_edit(self):
        import cards
        c = cards.card("p1")
        self.assertEqual((c["buy"], c["buyCur"], c["buyKzt"]), (9.5, "USD", 4493))     # в форме — доллары карточки
        self._ms("usd")
        r = self.c.post("/admin/api/cards/p1/edit", headers=self.h, json={"buy": 9.75}).get_json()["edit"]
        self.assertEqual(r["status"], "sent")
        self.assertEqual(self.sent[-1]["buyPrice"]["value"], 975)                      # центы, валюта карточки не тронута
        self.assertTrue(self.sent[-1]["buyPrice"]["currency"]["meta"]["href"].endswith("/usd"))
        with mirror.db() as d:
            self.assertEqual(d.run("SELECT buy_raw, buy_price FROM ms_product WHERE id='p1'", one=True), (9.75, round(9.75 * 472.9)))

    def test_currency_changed_in_moysklad_not_written(self):
        self._ms("kzt")                                                                 # в МойСклад закупку перевели в тенге
        r = self.c.post("/admin/api/cards/p1/edit", headers=self.h, json={"buy": 10}).get_json()["edit"]
        self.assertNotEqual(r["status"], "sent")
        self.assertEqual(self.sent, [])
