"""KPI сотрудников (модуль, 26.11.2026): прибыльность по сотрудникам МойСклад, бонус только с оплаченного, схемы
(% сверх базы, ступени, оклад), черновик не начисляет, сотрудник видит только свою карточку, ничего не пишет в МойСклад."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask  # noqa: E402

import admin  # noqa: E402
import inbox  # noqa: E402
import kpi  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402
import team  # noqa: E402

RUF = "f001c3c0-c9f6-11f0-0a80-0270000f3f89"
OWN = "64d45a72-168d-11f0-0a80-0db0000b9153"
EMP = "https://api.moysklad.ru/api/remap/1.2/entity/employee/"


def prow(eid, name, sell, cost, profit, count):
    return {"employee": {"meta": {"href": EMP + eid}, "name": name}, "sellSum": sell * 100, "sellCostSum": cost * 100,
            "returnSum": 0, "returnCostSum": 0, "profit": profit * 100, "salesCount": count}


class Kpi(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.profit = [prow(RUF, "Mussarov 2-й", 6_800_000, 5_400_000, 1_400_000, 44),
                       prow(OWN, "Mussarov A.", 1_000_000, 950_000, 50_000, 10)]
        self.demands = [{"owner": {"meta": {"href": EMP + RUF}}, "sum": 4_000_000 * 100, "payedSum": 4_000_000 * 100},
                        {"owner": {"meta": {"href": EMP + RUF}}, "sum": 1_000_000 * 100, "payedSum": 0}]

        def ms(method, path, params=None, **kw):
            self.calls.append((method, path, params))
            assert method == "GET", "KPI не пишет в МойСклад"
            if path == "/report/profit/byemployee":
                return {"rows": self.profit, "meta": {"size": len(self.profit)}}
            if path == "/entity/demand":
                return {"rows": self.demands, "meta": {"size": len(self.demands)}}
            if path == "/entity/retaildemand":
                return {"rows": [{"owner": {"meta": {"href": EMP + OWN}}, "sum": 100_000 * 100}], "meta": {"size": 1}}
            if path == "/entity/employee":
                return {"rows": [{"id": RUF, "name": "Mussarov 2-й"}], "meta": {"size": 1}}
            raise AssertionError(path)
        self.patch(oh, "ms", ms)
        self.me = {"role": "owner", "perms": [], "name": "В", "chat": "1"}
        self.patch(admin, "who", lambda: self.me)
        self.patch(team, "staff", lambda force=False: [{"chat": "77", "name": "Руфина", "active": True}])
        self.patch(kpi, "_now", lambda: datetime(2026, 11, 15, 12, 0, tzinfo=oh.ALMATY))
        kpi._cache.clear()
        with inbox.db() as d:
            inbox.set_setting(d, kpi.KEY, "")
            inbox.set_setting(d, "mod_kpi", "1")
        self.addCleanup(self.off)
        app = Flask(__name__)
        app.register_blueprint(kpi.bp)
        app.register_blueprint(admin.bp)
        self.c = app.test_client()

    @staticmethod
    def off():
        with inbox.db() as d:
            inbox.set_setting(d, "mod_kpi", "0")
            inbox.set_setting(d, kpi.KEY, "")

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def get(self, url):
        r = self.c.get(url)
        return r.status_code, json.loads(r.data)

    def put(self, b):
        r = self.c.put("/admin/api/kpi/settings", data=json.dumps(b), content_type="application/json")
        return r.status_code, json.loads(r.data)

    def card(self, o, eid):
        return next(c for c in o["cards"] if c["id"] == eid)

    def test_module_off_and_default_off(self):
        self.assertFalse(modules.REGISTRY["kpi"].get("default"))
        self.off()
        self.assertEqual(self.c.get("/admin/api/kpi").status_code, 404)

    def test_draft_ruf_facts_paid_share(self):
        code, o = self.get("/admin/api/kpi")
        self.assertEqual(code, 200)
        r = self.card(o, RUF)
        self.assertTrue(r["draft"])
        self.assertEqual((r["vp"], r["revenue"], r["count"], r["margin"]), (1_400_000, 6_800_000, 44, 20.6))
        self.assertEqual((r["paid"], r["debt"], r["nShip"], r["nPaid"]), (4_000_000, 1_000_000, 2, 1))
        self.assertEqual(r["vpPaid"], 1_120_000)                        # 1,4 млн × 4/5 оплачено
        self.assertEqual(r["bonus"], 0)                                  # черновик не начисляет
        self.assertEqual(r["bonusCalc"], 3000)                           # (1 120 000 − 1 100 000) × 15 %
        self.assertEqual((r["salary"], r["total"]), (250000, 250000))
        self.assertIsNone(r["pct"])                                      # план на ноябрь не задан (черновик — декабрь)
        self.assertEqual(r["forecast"], 2_800_000)                       # 1,4 млн за 15 дней из 30
        own = self.card(o, OWN)
        self.assertFalse(own["setup"])
        self.assertTrue(own["lowMargin"])                                # 5 % < 8 %
        self.assertEqual(own["paid"], 100_000)                           # розничная продажа — оплачена
        self.assertTrue(all(c[0] == "GET" for c in self.calls))

    def test_schemes_and_plan(self):
        self.assertEqual(self.put({"id": RUF, "draft": False, "month": "2026-11", "plan": "1 000 000"})[0], 200)
        r = self.card(self.get("/admin/api/kpi")[1], RUF)
        self.assertEqual((r["bonus"], r["total"], r["pct"]), (3000, 253000, 140.0))
        self.put({"id": RUF, "scheme": "steps", "steps": [{"at": 100, "sum": 50000}, {"at": "120", "sum": "90000"}, {"at": "", "sum": ""}]})
        kpi._cache.clear()
        r = self.card(self.get("/admin/api/kpi")[1], RUF)
        self.assertEqual(r["bonus"], 50000)                              # оплаченная ВП 1,12 млн = 112 % → ступень 100 %
        self.put({"id": RUF, "scheme": "none"})
        r = self.card(self.get("/admin/api/kpi")[1], RUF)
        self.assertEqual((r["bonus"], r["total"]), (0, 250000))
        self.assertEqual(kpi.plan_for({"plans": {"2026-10": 5, "2026-12": 9}}, "2026-11"), 5)
        self.assertEqual(self.put({"id": RUF, "pct": "150"})[0], 400)
        self.assertEqual(self.put({"id": RUF, "salary": "abc"})[0], 400)

    def test_staff_sees_only_own(self):
        self.put({"id": RUF, "chat": "77"})
        self.me = {"role": "staff", "perms": ["orders"], "name": "Руфина", "chat": "77"}
        self.assertEqual(self.c.get("/admin/api/kpi").status_code, 403)          # владелец ещё не открыл
        self.assertEqual(self.put({"staff": True})[0], 403)                      # настройки — только владелец
        self.me = {"role": "owner", "perms": [], "name": "В", "chat": "1"}
        self.put({"staff": True})
        on = json.loads(self.c.get("/admin/api/modules/on").data)["on"]
        self.assertTrue(on.get("kpi_staff"))
        self.me = {"role": "staff", "perms": ["orders"], "name": "Руфина", "chat": "77"}
        code, o = self.get("/admin/api/kpi")
        self.assertEqual((code, len(o["cards"])), (200, 1))
        c = o["cards"][0]
        self.assertEqual(c["id"], RUF)
        for k in ("revenue", "margin", "salary", "total", "debt", "count"):
            self.assertNotIn(k, c)                                               # цифры компании и оклад не видит
        self.assertNotIn("company", o)
        self.assertEqual(self.c.get("/admin/api/kpi/history?id=" + OWN).status_code, 403)
        self.me = {"role": "staff", "perms": ["orders"], "name": "Другой", "chat": "99"}
        self.assertEqual(self.get("/admin/api/kpi")[1]["cards"], [])

    def test_history_and_bounds(self):
        code, h = self.get("/admin/api/kpi/history?id=" + RUF)
        self.assertEqual((code, len(h["items"]), h["items"][0]["month"]), (200, 6, "2026-11"))
        frm, to, days = kpi._month_bounds("2026-12")
        self.assertEqual((frm, to, days), ("2026-11-30 22:00:00", "2026-12-31 21:59:59", 31))


if __name__ == "__main__":
    unittest.main()
