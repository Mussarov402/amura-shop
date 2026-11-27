"""KPI сотрудников (модуль, 26.11.2026; уточнение владельца — «считать от сотрудника в нашей системе»):
заказы закрепляются за тем, кто создал/провёл их в системе AMURA; переходный период — доп. логин МойСклад без двойного
счёта; ВП = выручка отгрузок − себестоимость; бонус только с оплаченного; схемы; черновик не начисляет; сотрудник
с правом «kpi» видит только свою карточку; ничего не пишет в МойСклад."""
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
import retail_econ  # noqa: E402
import team  # noqa: E402

ADM2 = "f001c3c0-c9f6-11f0-0a80-0270000f3f89"
OWN = "64d45a72-168d-11f0-0a80-0db0000b9153"
EMP = "https://api.moysklad.ru/api/remap/1.2/entity/employee/"
RUF = "77"


def doc(did, order, owner, total, paid):
    return {"id": did, "customerOrder": {"name": order} if order else None, "owner": {"meta": {"href": EMP + owner}},
            "sum": total * 100, "payedSum": paid * 100}


def pos(pid, qty, service=False):
    return {"assortment": {"meta": {"href": "https://x/entity/product/" + pid, "type": "service" if service else "product"}},
            "quantity": qty}


class Kpi(unittest.TestCase):
    def setUp(self):
        self.calls = []
        # d1 — заказ 3001 создан Руфиной в системе (под API-пользователем); d2 — её продажа прямо в МС под admin2;
        # d3 — заказ 3002 оформлен в МС под admin2, но «Собран» в системе поставил другой сотрудник (88) — у него, не у неё;
        # d4 — заказ владельца, не её; d5 — заказ 3001 ещё одна отгрузка в долг
        self.demands = [doc("d1", "3001", OWN, 100_000, 100_000), doc("d2", "", ADM2, 50_000, 50_000),
                        doc("d3", "3002", ADM2, 40_000, 40_000), doc("d4", "3003", OWN, 70_000, 70_000),
                        doc("d5", "3001", OWN, 30_000, 0)]
        self.positions = {"d1": [pos("p1", 2), pos("srv", 1, True)], "d2": [pos("p2", 1)], "d3": [pos("p1", 1)],
                          "d4": [pos("p1", 1)], "d5": [pos("p1", 1)], "r1": [pos("p2", 2)]}

        def ms(method, path, params=None, **kw):
            self.calls.append((method, path, params))
            assert method == "GET", "KPI не пишет в МойСклад"
            if path == "/report/profit/byemployee":
                return {"rows": [{"employee": {"meta": {"href": EMP + OWN}}, "sellSum": 290_000_00, "returnSum": 0, "profit": 15_000_00}],
                        "meta": {"size": 1}}
            if path == "/entity/demand":
                return {"rows": self.demands, "meta": {"size": len(self.demands)}}
            if path == "/entity/retaildemand":
                return {"rows": [{"id": "r1", "owner": {"meta": {"href": EMP + ADM2}}, "sum": 20_000 * 100}], "meta": {"size": 1}}
            if path.endswith("/positions"):
                rows = self.positions[path.split("/")[3]]
                return {"rows": rows, "meta": {"size": len(rows)}}
            if path == "/entity/employee":
                return {"rows": [{"id": ADM2, "name": "Mussarov 2-й"}], "meta": {"size": 1}}
            raise AssertionError(path)
        self.patch(oh, "ms", ms)
        self.patch(retail_econ, "_cost", lambda pid: ({"p1": 30_000, "p2": 20_000}.get(pid), ""))
        self.me = {"role": "owner", "perms": list(team.PERMS), "name": "В", "chat": "1"}
        self.patch(admin, "who", lambda: self.me)
        self.patch(team, "staff", lambda force=False: [{"chat": RUF, "name": "Руфина", "active": True, "perms": ["orders", "kpi"]},
                                                       {"chat": "88", "name": "Ержан", "active": True, "perms": ["orders"]}])
        self.patch(kpi, "_now", lambda: datetime(2026, 11, 15, 12, 0, tzinfo=oh.ALMATY))
        self.spawned = []
        self.patch(kpi, "_spawn", lambda fn, *a: (self.spawned.append(a), fn(*a)))   # фон — сразу, в тестах
        kpi._cache.clear()
        kpi._running.clear()
        kpi._errors.clear()
        with inbox.db() as d:
            kpi._table(d)
            d.run("DELETE FROM kpi_order")
            inbox.set_setting(d, kpi.KEY, "")
            inbox.set_setting(d, "mod_kpi", "1")
            d.run("DELETE FROM setting WHERE key LIKE %s", (kpi.SNAP + "%",))
        self.addCleanup(self.off)
        kpi.attribute("3001", RUF, "Руфина", "создал заказ")
        kpi.attribute("3001", "88", "Ержан", "статус «Собран»")              # второй не перебивает первого
        kpi.attribute("3002", "88", "Ержан", "статус «Собран»")
        kpi.attribute("3003", "1", "Владелец", "статус «Подтвержден»")
        app = Flask(__name__)
        app.register_blueprint(kpi.bp)
        self.c = app.test_client()

    @staticmethod
    def off():
        with inbox.db() as d:
            inbox.set_setting(d, "mod_kpi", "0")
            inbox.set_setting(d, kpi.KEY, "")
            d.run("DELETE FROM kpi_order")
            d.run("DELETE FROM setting WHERE key LIKE %s", (kpi.SNAP + "%",))

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

    def ruf(self):
        kpi._cache.clear()
        return next(c for c in self.get("/admin/api/kpi?refresh=1")[1]["cards"] if c["id"] == "ruf")

    def link(self, **kw):
        self.assertEqual(self.put({"id": "ruf", "chat": RUF, **kw})[0], 200)

    def test_module_off_and_perm(self):
        self.assertFalse(modules.REGISTRY["kpi"].get("default"))
        self.assertIn("kpi", team.PERMS)
        self.off()
        self.assertEqual(self.c.get("/admin/api/kpi").status_code, 404)

    def test_attribution_no_double_count(self):
        self.link()
        r = self.ruf()
        # d1 + d5 (заказ 3001 закреплён за ней) + d2 и r1 (admin2, без заказа в системе); d3 — у Ержана, d4 — у владельца
        self.assertEqual((r["count"], r["revenue"], r["fromSystem"]), (4, 200_000, 2))
        self.assertEqual(r["cost"], 60_000 + 30_000 + 20_000 + 40_000)          # услуга без себестоимости не считается
        self.assertEqual((r["vp"], r["paid"], r["debt"], r["nPaid"]), (50_000, 170_000, 30_000, 3))
        self.assertEqual(r["vpPaid"], 42_500)                                   # 50 000 × 170/200
        self.assertEqual(r["margin"], 25.0)
        self.assertTrue(all(c[0] == "GET" for c in self.calls))

    def test_without_ms_login_only_system_orders(self):
        self.link(msExtra="")
        r = self.ruf()
        self.assertEqual((r["count"], r["revenue"]), (2, 130_000))

    def test_draft_and_schemes(self):
        self.link()
        r = self.ruf()
        self.assertTrue(r["draft"])
        self.assertEqual((r["bonus"], r["bonusCalc"], r["salary"], r["total"]), (0, 0, 250000, 250000))   # 42 500 < базы 1,1 млн
        self.link(draft=False, base=0, month="2026-11", plan="50000")
        r = self.ruf()
        self.assertEqual((r["bonus"], r["total"], r["pct"]), (6375, 256375, 100.0))   # 42 500 × 15 %
        self.link(scheme="steps", steps=[{"at": 80, "sum": 10000}, {"at": "100", "sum": "20000"}, {"at": "", "sum": ""}])
        self.assertEqual(self.ruf()["bonus"], 10000)                             # оплаченная ВП 42 500 = 85 % плана
        self.link(scheme="none")
        self.assertEqual(self.ruf()["bonus"], 0)
        self.assertEqual(self.put({"id": "ruf", "pct": "150"})[0], 400)
        self.assertEqual(self.put({"chat": RUF, "name": "Дубль"})[0], 400)       # один сотрудник — одна карточка
        self.assertEqual(self.put({"name": "Никто"})[0], 400)                   # без сотрудника системы и логина МС
        code, o = self.put({"chat": "88", "name": "Ержан", "scheme": "none"})
        self.assertEqual(code, 200)
        e = next(c for c in self.get("/admin/api/kpi")[1]["cards"] if c["id"] == o["id"])   # новый сотрудник — пересчёт сам
        self.assertEqual((e["count"], e["revenue"]), (1, 40_000))                # заказ 3002 из МС, проведённый им в системе

    def test_staff_sees_only_own(self):
        self.link()
        self.me = {"role": "staff", "perms": ["orders", "kpi"], "name": "Руфина", "chat": RUF}
        code, o = self.get("/admin/api/kpi")
        self.assertEqual((code, len(o["cards"]), o["cards"][0]["id"]), (200, 1, "ruf"))
        for k in ("revenue", "margin", "salary", "total", "debt", "count", "cost"):
            self.assertNotIn(k, o["cards"][0])
        self.assertNotIn("company", o)
        self.assertEqual(self.put({"id": "ruf", "salary": 1})[0], 403)
        self.assertEqual(self.c.get("/admin/api/kpi/history?id=ruf").status_code, 403)
        self.me = {"role": "staff", "perms": ["orders"], "name": "Ержан", "chat": "88"}
        self.assertEqual(self.c.get("/admin/api/kpi").status_code, 403)         # нет права «KPI»

    def test_mark_from_panel_and_history(self):
        kpi.mark("3009", "создал заказ", {"chat": RUF, "name": "Руфина"})
        kpi.mark("3010", "статус", {})                                          # без входа — ничего
        a = kpi.attributed()
        self.assertEqual((a.get("3009"), a.get("3001"), "3010" in a), (RUF, RUF, False))
        self.link()
        code, h = self.get("/admin/api/kpi/history?id=ruf")
        self.assertEqual((code, len(h["items"]), h["items"][0]["month"]), (200, 6, "2026-11"))
        self.assertEqual(kpi._month_bounds("2026-12"), ("2026-11-30 22:00:00", "2026-12-31 21:59:59", 31))

    def test_panel_state_marks_order(self):
        """«Собран» из системы AMURA закрепляет заказ за тем, кто вошёл (если его ещё никто не создал/не провёл)."""
        puts = []
        st = {"meta": {"href": "https://x/state/s1"}}

        def ms(method, path, params=None, json=None, **kw):
            if path == "/entity/customerorder/metadata":
                return {"states": [{"name": "Собран", **st}, {"name": "Новый", "meta": {"href": "https://x/state/s0"}}]}
            if method == "PUT":
                puts.append(path)
                return {}
            raise AssertionError(path)
        self.patch(oh, "ms", ms)
        self.patch(admin, "fresh", lambda key, ttl, fn: fn())
        self.me = {"role": "staff", "perms": ["orders"], "name": "Руфина", "chat": RUF}
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        c = app.test_client()
        oid = "11111111-2222-3333-4444-555555555555"
        for num, state in (("4001", "Собран"), ("4002", "Новый"), ("3002", "Собран")):
            r = c.post("/admin/api/orders/state", json={"number": num, "state": state, "id": oid})
            self.assertEqual(r.status_code, 200)
        a = kpi.attributed()
        self.assertEqual((a.get("4001"), "4002" in a, a.get("3002")), (RUF, False, "88"))   # 3002 уже у Ержана
        self.assertEqual(len(puts), 3)

    def test_background_snapshot_fast_answer(self):
        """Экран отвечает сразу: без снимка — «Считаю» (ready=False), пересчёт в фоне; дальше — из снимка без МойСклад."""
        self.link()
        later = []
        self.patch(kpi, "_spawn", lambda fn, *a: later.append((fn, a)))
        code, o = self.get("/admin/api/kpi")
        self.assertEqual((code, o["ready"], o["computing"], o["cards"]), (200, False, True, []))
        self.assertFalse([c for c in self.calls if c[1] != "/report/profit/byemployee"])      # запрос экрана МС не ждёт
        self.get("/admin/api/kpi")
        self.assertEqual(len(later), 1)                                                         # второй расчёт не запускается
        fn, a = later.pop()
        fn(*a)
        n = len(self.calls)
        code, o = self.get("/admin/api/kpi")
        self.assertEqual((o["ready"], o["computing"], len(o["cards"])), (True, False, 1))
        self.assertEqual(len(self.calls), n)                                                    # из снимка, без МойСклад
        self.assertTrue(o["at"])
        self.put({"id": "ruf", "month": "2026-11", "plan": "10"})                               # план — без пересчёта
        self.assertFalse(self.get("/admin/api/kpi")[1]["computing"])
        self.assertEqual(self.get("/admin/api/kpi")[1]["cards"][0]["plan"], 10)
        self.put({"id": "ruf", "msExtra": ""})                                                  # кто чьи продажи — пересчёт
        self.assertTrue(self.get("/admin/api/kpi")[1]["computing"])
        later.clear()
        kpi._running.clear()
        self.assertTrue(self.get("/admin/api/kpi?refresh=1")[1]["computing"])                   # «Обновить»
        self.assertEqual(len(later), 1)

    def test_failed_compute_shows_error_not_crash(self):
        def bad(*a, **k):
            raise RuntimeError("МойСклад 503")
        self.patch(kpi, "_docs", bad)
        code, o = self.get("/admin/api/kpi")
        self.assertEqual((code, o["ready"]), (200, False))
        self.assertIn("503", o["error"])
        n = len(self.spawned)
        self.assertFalse(self.get("/admin/api/kpi")[1]["computing"])                            # не долбим МС каждый запрос
        self.assertEqual(len(self.spawned), n)

    def test_positions_and_costs_cached_per_product(self):
        self.link()
        self.ruf()
        prods = []
        self.patch(retail_econ, "_cost", lambda pid: (prods.append(pid), ({"p1": 30_000, "p2": 20_000}.get(pid), ""))[1])
        kpi._cache.clear()
        kpi.compute("2026-11")
        self.assertEqual(sorted(prods), ["p1", "p2"])                                           # себестоимость — раз на товар


if __name__ == "__main__":
    unittest.main()
