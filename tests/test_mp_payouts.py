"""Выплаты площадок → МойСклад (схема Финансиста): приход по ценам продаж с привязкой к отгрузкам, удержания по статьям,
сверка приход − возвраты − удержания = выплата, возвраты на основании отгрузки, рубли по курсу, без повторов."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_marketplaces as tm  # noqa: E402

import marketplaces as mpk  # noqa: E402
import mp_calc  # noqa: E402
import mp_payouts as po  # noqa: E402
import order_hook as oh  # noqa: E402

WB_ROWS = [
    {"realizationreport_id": 777, "create_dt": "2026-11-03", "currency_name": "KZT", "doc_type_name": "Продажа", "supplier_oper_name": "Продажа",
     "assembly_id": 5, "retail_price_withdisc_rub": 10000, "ppvz_for_pay": 7500, "acquiring_fee": 250, "rrd_id": 1},
    {"realizationreport_id": 777, "create_dt": "2026-11-03", "currency_name": "KZT", "doc_type_name": "Продажа", "supplier_oper_name": "Продажа",
     "assembly_id": 6, "retail_price_withdisc_rub": 5000, "ppvz_for_pay": 3750, "acquiring_fee": 125, "rrd_id": 2},
    {"realizationreport_id": 777, "create_dt": "2026-11-03", "currency_name": "KZT", "doc_type_name": "", "supplier_oper_name": "Логистика",
     "delivery_rub": 900, "rrd_id": 3},
    {"realizationreport_id": 777, "create_dt": "2026-11-03", "currency_name": "KZT", "doc_type_name": "", "supplier_oper_name": "Хранение",
     "storage_fee": 100, "rrd_id": 4},
    {"realizationreport_id": 777, "create_dt": "2026-11-03", "currency_name": "KZT", "doc_type_name": "Возврат", "supplier_oper_name": "Возврат",
     "assembly_id": 9, "retail_price_withdisc_rub": 2000, "ppvz_for_pay": 1500, "acquiring_fee": 50, "rrd_id": 5},
]


class Payouts(unittest.TestCase):
    patch = tm.Channels.patch

    def setUp(self):
        tm.Channels.setUp(self)
        self.posts = []
        orders = {"wb-5": ("1901", "d5"), "wb-9": ("1880", "d9")}                     # wb-6 нет в МойСклад
        self.returns = {"d9": "sr9"}                                                 # возврат покупателя по заказу 1880 оформлен

        def ms(method, path, **kw):
            q = (kw.get("params") or {}).get("filter", "")
            if path == "/entity/customerorder" and method == "GET":
                o = orders.get(q.split("=", 1)[1])
                return {"rows": [{"name": o[0], "demands": [{"meta": {"href": f"https://x/demand/{o[1]}"}}]}] if o else []}
            if method == "GET" and path in ("/entity/paymentin", "/entity/paymentout", "/entity/salesreturn"):
                return {"rows": [p for p in self.posts if p[0] == path and (q.split("=", 1)[1] in (p[1].get("externalCode"), p[1].get("incomingNumber")))]}
            if path == "/entity/expenseitem" and method == "GET":
                return {"rows": []}
            if path == "/entity/expenseitem" and method == "POST":
                return {"id": "item-" + kw["json"]["name"]}
            if path.startswith("/entity/demand/") and method == "GET":
                sr = self.returns.get(path.rsplit("/", 1)[1])
                return {"returns": [{"meta": {"href": f"https://x/salesreturn/{sr}"}}] if sr else []}
            if method == "POST":
                self.posts.append((path, kw["json"]))
                return {"name": str(len(self.posts))}
            raise AssertionError((method, path))
        self.patch(oh, "ms", ms)
        self.patch(mpk.WB, "req", lambda s, m, base, path, **kw: WB_ROWS if kw["params"]["rrdid"] == 0 else [])

    def test_wb_plan_and_apply_once(self):
        ch = mpk.CHANNELS["wb"]
        pl = po.list_plans(ch)[0]
        self.assertTrue(pl["ok"], pl.get("why"))
        self.assertEqual((pl["income"], pl["payoutKzt"]), (15000, 8750))             # продажи по ценам; к выплате 11250−900−100−1500
        self.assertEqual(pl["outs"], {"МП: комиссия": 2925, "МП: эквайринг": 325, "МП: логистика и доставка": 900, "МП: хранение": 100})
        self.assertEqual(pl["missing"], ["6"])
        with self.assertRaises(ValueError):                                          # счёт не выбран — проводить нельзя
            po.apply(ch, pl)
        mpk.set_setting("mp_payout_account", "acc-1")
        self.addCleanup(lambda: mpk.set_setting("mp_payout_account", ""))
        made = po.apply(ch, pl, "Владелец")
        pin = next(p[1] for p in self.posts if p[0] == "/entity/paymentin")
        self.assertEqual((pin["sum"], pin["incomingNumber"], [o["linkedSum"] for o in pin["operations"]]), (1500000, "777", [1000000]))
        self.assertIn("Не найдены в МойСклад: 6", pin["description"])
        outs = {p[1]["expenseItem"]["meta"]["href"].rsplit("/", 1)[1]: p[1]["sum"] for p in self.posts if p[0] == "/entity/paymentout"}
        self.assertEqual(outs["item-МП: комиссия"], 292500)
        self.assertFalse(any(p[0] == "/entity/salesreturn" for p in self.posts))     # возврат покупателя оформляет склад, не система
        ret = [p[1] for p in self.posts if p[0] == "/entity/paymentout" and p[1]["expenseItem"]["meta"]["href"].endswith(po.RETURN_ITEM_ID)]
        self.assertEqual((ret[0]["sum"], ret[0]["operations"][0]["meta"]["href"].rsplit("/", 1)[1]), (200000, "sr9"))   # 15000 − 2000 − 4250 = 8750
        self.assertEqual(len(made), 6)
        n = len(self.posts)
        self.assertTrue(po.list_plans(ch)[0]["done"])
        po.apply(ch, pl)
        self.assertEqual(len(self.posts), n)                                        # повтор — ни одного нового документа

    def test_needs_salesreturn_and_compensation(self):
        self.returns.clear()
        pl = po.list_plans(mpk.CHANNELS["wb"])[0]
        self.assertFalse(pl["ok"])
        self.assertIn("нужен возврат по заказу №1880", pl["why"])
        rows = WB_ROWS + [{"realizationreport_id": 777, "create_dt": "2026-11-03", "currency_name": "KZT", "doc_type_name": "",
                           "supplier_oper_name": "Компенсация ущерба", "additional_payment": 700, "rrd_id": 6}]
        self.patch(mpk.WB, "req", lambda s, m, base, path, **kw: rows if kw["params"]["rrdid"] == 0 else [])
        self.returns["d9"] = "sr9"
        pl = po.list_plans(mpk.CHANNELS["wb"])[0]
        self.assertTrue(pl["ok"], pl["why"])
        self.assertEqual((pl["income"], pl["compKzt"], pl["payoutKzt"]), (15700, 700, 9450))   # компенсация — в приход

    def test_mismatch_and_rub(self):
        p = {"id": "x", "date": "2026-11-03", "cur": "RUB", "sales": [], "returns": [], "out": {k: 0.0 for k in po.ITEMS}, "payout": 0.0, "comp": 0.0}
        p["out"]["commission"] = 10
        p["payout"] = 5                                                              # 0 − 10 ≠ 5
        self.patch(mp_calc, "to_kzt", lambda v, cur, *a, **k: (v * 5.5, "курс покупки МиГ"))
        pl = po.plan(mpk.CHANNELS["ozon"], p)
        self.assertFalse(pl["ok"])
        self.assertIn("не сходится", pl["why"])
        self.assertTrue(pl["rateNote"].startswith("оценка"))
        self.assertEqual(pl["outs"], {"МП: комиссия": 55})

    def test_ozon_period(self):
        def call(s, path, body):
            if path == "/v1/finance/cash-flow-statement/list":
                return {"result": {"cash_flows": [{"period": {"id": 9001, "begin": "2026-10-16", "end": "2026-10-31"}, "currency_code": "KZT"}],
                                   "page_count": 1}}
            return {"result": {"page_count": 1, "operations": [
                {"posting": {"posting_number": "P-1"}, "accruals_for_sale": 5790, "sale_commission": -695, "amount": 4595,
                 "services": [{"name": "MarketplaceServiceItemDirectFlowLogistic", "price": -400},
                              {"name": "MarketplaceServiceItemRedistributionLastMileCourier", "price": -100}]},
                {"posting": {"posting_number": ""}, "accruals_for_sale": 0, "sale_commission": 0, "amount": -300,
                 "operation_type_name": "Оплата за клик", "services": []},
                {"posting": {"posting_number": ""}, "accruals_for_sale": 0, "sale_commission": 0, "amount": -50,
                 "services": [{"name": "OperationMarketplaceServiceStorage", "price": -50}]}]}}
        self.patch(mpk.Ozon, "call", call)
        p = po.ozon_payouts(mpk.CHANNELS["ozon"], "2026-10-01", "2026-11-04")["9001"]
        self.assertEqual((p["payout"], p["sales"]), (4245, [{"order": "P-1", "sum": 5790}]))
        self.assertEqual({k: v for k, v in p["out"].items() if v}, {"commission": 695, "logistics": 500, "ads": 300, "storage": 50})
        pl = po.plan(mpk.CHANNELS["ozon"], p)
        self.assertTrue(pl["ok"], pl["why"])                                          # 5790 − 695 − 500 − 300 − 50 = 4245


if __name__ == "__main__":
    unittest.main()
