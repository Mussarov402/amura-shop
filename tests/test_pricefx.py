"""Цены МС в валюте (17.11.2026): «Короб» = 5,40 $ читался как 5 ₸. Теперь — в тенге по курсу МС, тир показывается в $,
в заказ — строка «Короб $5,40 × курс 472,9 = … ₸»; страховка: тир < 100 ₸ при опте от 100 ₸ без fx не продаём."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import order_hook as oh  # noqa: E402
import pricefx  # noqa: E402

KZT, USD = "64e84e60-0000-0000-0000-00000000k000", "7b014f4a-0000-0000-0000-00000000u000"
CUR = [{"id": KZT, "isoCode": "KZT", "rate": 1, "multiplicity": 1}, {"id": USD, "isoCode": "USD", "rate": 472.9, "multiplicity": 1}]
NAMES = (oh.PRICE_OPT, oh.PRICE_MID, oh.PRICE_BOX)


def sp(name, value, cur):
    return {"value": value, "currency": {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/currency/{cur}"}},
            "priceType": {"name": name}}


def row(box_value, box_cur=USD, opt=255000, mid=250000):
    return {"id": "p1", "code": "00549", "name": "Маска", "packs": [{"quantity": 40}],
            "salePrices": [sp(oh.PRICE_OPT, opt, KZT), sp("От 10шт", mid, KZT), sp(oh.PRICE_BOX, box_value, box_cur),
                           sp(oh.PRICE_RTL, 399000, KZT)]}


class Fx(unittest.TestCase):
    def setUp(self):
        self.cm = pricefx.cur_map(CUR)

    def test_usd_tier_converted_not_read_as_tenge(self):
        self.assertEqual(pricefx.sale(row(540), oh.PRICE_BOX, self.cm), (2554, {"iso": "USD", "v": 5.4, "rate": 472.9}))
        opt, mid, box, bq, fx = pricefx.tiers(row(540), self.cm, NAMES, 40)
        self.assertEqual((opt, mid, box, bq, fx), (2550, 2500, 0, 0, {}))             # 2 554 ₸ дороже опта — короба нет, но и не 5 ₸
        opt, mid, box, bq, fx = pricefx.tiers(row(500), self.cm, NAMES, 40)
        self.assertEqual((box, bq, fx["box"]["v"]), (2364, 40, 5.0))                  # $5 = 2 364 ₸ — дешевле опта, есть

    def test_unknown_currency_no_price(self):
        self.assertEqual(pricefx.sale(row(540, box_cur="nope"), oh.PRICE_BOX, self.cm), (0, None))
        self.assertEqual(pricefx.sale(row(540), oh.PRICE_BOX, {}), (0, None))         # справочника нет — не продаём

    def test_reprice_and_guard(self):
        it = {"opt": 2550, "mid": 2500, "box": 2364, "boxQty": 40, "fx": {"box": {"iso": "USD", "v": 5.0}}}
        pricefx.reprice(it, {"USD": 480.0})
        self.assertEqual((it["box"], it["fx"]["box"]["rate"]), (2400, 480.0))         # курс МС сейчас
        pricefx.reprice(it, {"USD": 520.0})
        self.assertEqual((it["box"], it["fx"]), (0, {}))                              # стал дороже опта — убрали
        old = {"opt": 2550, "mid": 2500, "box": 5, "boxQty": 40}                      # старая выгрузка без fx: 5,40 $ → 5 ₸
        self.assertEqual(pricefx.guard(old), ["box"])
        self.assertEqual(oh.unit_price(old, 40), 2500)                               # короб по 5 ₸ не продаётся

    def test_note(self):
        self.assertEqual(pricefx.note("Маска", "box", {"iso": "USD", "v": 5.4, "rate": 472.9}, 2554),
                         "Короб $5,40 × курс 472,9 = 2 554 ₸ — Маска")


class LiveAndOrder(unittest.TestCase):
    """Живой каталог из строки МС с «Короб» в $ и заказ: смешанный ₸ + $, строка курса в описании заказа."""

    def setUp(self):
        self.posted = []
        saved = {k: getattr(oh, k) for k in ("ms",)}
        self.addCleanup(lambda: [setattr(oh, k, v) for k, v in saved.items()])
        for k in ("currencies", "currencies_last"):
            oh._cache.pop(k, None)
        self.addCleanup(lambda: [oh._cache.pop(k, None) for k in ("currencies", "currencies_last")])

        def ms(method, path, **kw):
            if path == "/entity/currency":
                return {"rows": CUR}
            raise AssertionError(path)
        oh.ms = ms

    def test_build_live_usd_box(self):
        r = row(500)
        data = oh.build_live([r, {**row(540), "id": "p2", "code": "00550"}], stock={"p1": 100, "p2": 100})
        oh._fx_fix(data)
        a, b = sorted(data["items"], key=lambda i: i["code"])
        self.assertEqual((a["opt"], a["mid"], a["box"], a["boxQty"]), (2550, 2500, 2364, 40))
        self.assertEqual(a["fx"], {"box": {"iso": "USD", "v": 5.0, "rate": 472.9}})
        self.assertEqual((b["box"], b.get("fx")), (0, None))                          # $5,40 → 2 554 ₸ ≥ опта — короба нет
        self.assertEqual(a["rtl"], 3990)
        self.assertEqual(oh.unit_price(a, 40), 2364)
        self.assertEqual(oh.unit_price(a, 12), 2500)

    def test_site_row_repriced_and_guarded(self):
        data = {"items": [{"id": "s1", "code": "1", "opt": 2550, "mid": 0, "box": 2364, "boxQty": 40, "fx": {"box": {"iso": "USD", "v": 5.0}}},
                          {"id": "s2", "code": "2", "opt": 2550, "mid": 0, "box": 5, "boxQty": 40}]}
        oh._fx_fix(data)
        s1, s2 = data["items"]
        self.assertEqual((s1["box"], s1["fx"]["box"]["rate"]), (2364, 472.9))
        self.assertEqual(s2["box"], 0)


class MixedOrder(unittest.TestCase):
    """Заказ: крем (₸) + маска коробом (тир в $) → суммы в ₸, в описании заказа строка курса только у маски."""

    def setUp(self):
        self.posted = []
        cat = {"p1": {"id": "p1", "name": "Крем", "qty": 50, "opt": 5000, "mid": 4900, "box": 0, "boxQty": 0, "rtl": 6690},
               "p2": {"id": "p2", "name": "Маска", "qty": 100, "opt": 2550, "mid": 2500, "box": 2364, "boxQty": 40, "rtl": 3990,
                      "fx": {"box": {"iso": "USD", "v": 5.0, "rate": 472.9}}}}

        def ms(method, path, **kw):
            if path.startswith("/entity/counterparty/"):
                return {"tags": []}
            if path == "/entity/customerorder":
                return {"meta": {"size": 1}, "rows": []}
            raise AssertionError(path)
        for name, val in {"ms": ms, "catalog": lambda: cat, "too_many": lambda *a, **k: False,
                          "find_or_create_agent": lambda *a: "agent-1", "organization": lambda: "org-1", "store_id": lambda: "store-1",
                          "fee_service_id": lambda: "fee-1", "delivery_service_id": lambda: "dlv-1", "notify_bg": lambda fn: None,
                          "is_wholesale": lambda cid: True, "cp_by_phone": lambda phone: None,
                          "_post_order": lambda body: self.posted.append(body) or {"name": "1600", "id": "o1"}}.items():
            old = getattr(oh, name)
            setattr(oh, name, val)
            self.addCleanup(lambda n=name, o=old: setattr(oh, n, o))

    def test_mixed(self):
        d = {"name": "К", "phone": "+77010000000", "city": "Алматы", "shipping": "pickup",
             "items": [{"id": "p1", "qty": 3}, {"id": "p2", "qty": 40}]}
        res, st = oh.order_core(d, "kfx1", "1.1.1.1", "cid-1")
        self.assertEqual(st, 200, res)
        body = self.posted[-1]
        prices = sorted(p["price"] for p in body["positions"] if p["assortment"]["meta"]["type"] == "product")
        self.assertEqual(prices, [236400, 500000])                                      # всё в тиынах ₸: маска 2 364 ₸, крем 5 000 ₸
        self.assertIn("Короб $5,00 × курс 472,9 = 2 364 ₸ — Маска", body["description"])
        self.assertEqual(body["description"].count("× курс"), 1)                       # у крема (₸) строки нет
        d["items"] = [{"id": "p2", "qty": 12}]
        oh.order_core(d, "kfx2", "1.1.1.1", "cid-1")
        self.assertNotIn("× курс", self.posted[-1]["description"])                     # от 10 шт — в ₸, строки нет


if __name__ == "__main__":
    unittest.main()
