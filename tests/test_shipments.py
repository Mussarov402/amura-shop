"""Отгрузка из заказа в системе: шаблон МойСклад по заказу, без дублей со сценарием, флаг по умолчанию выключен."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401
import admin  # noqa: E402
import app  # noqa: E402
import inbox  # noqa: E402
import order_hook as oh  # noqa: E402
import requests  # noqa: E402
import shipments  # noqa: E402

OID = "11111111-2222-3333-4444-555555555555"


class FakeShipMS:
    def __init__(self):
        self.demands, self.calls, self.fail, self.order_demands = [], [], [], []

    def __call__(self, method, path, params=None, json=None, **kw):
        self.calls.append((method, path, params, json))
        if path == f"/entity/customerorder/{OID}":
            return {"id": OID, "name": "00077", "demands": [{"meta": {"href": f"https://x/entity/demand/{d}"}} for d in self.order_demands]}
        if path == "/entity/demand/new" and method == "PUT":
            assert json["customerOrder"]["meta"]["href"].endswith(OID)
            return {"customerOrder": json["customerOrder"], "positions": [{"quantity": 2, "price": 150000}], "description": "из заказа"}
        if path == "/entity/demand" and method == "GET":
            code = params["filter"].split("externalCode=")[-1]
            return {"rows": [d for d in self.demands if d["externalCode"] == code]}
        if path == "/entity/demand" and method == "POST":
            doc = {"id": f"dm{len(self.demands) + 1}", "name": f"{len(self.demands) + 1:05d}", **json}
            if self.fail:
                e = self.fail.pop(0)
                if e == "lost":
                    self.demands.append(doc)
                    self.order_demands.append(doc["id"])
                    raise requests.ConnectionError("timeout")
                raise e
            self.demands.append(doc)
            self.order_demands.append(doc["id"])
            return doc
        raise AssertionError(f"неожиданный запрос {method} {path}")


class ShipmentsTest(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        self.ms = oh.ms = FakeShipMS()
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + admin.make_admin_token()}
        with inbox.db() as d:
            d.run("DELETE FROM setting WHERE key='feat_ship_ops'")
        with shipments.db() as d:
            d.run("DELETE FROM ship_op")

    def ship(self):
        return self.c.post(f"/admin/api/orders/00077/ship?id={OID}", headers=self.h)

    def on(self):
        self.c.post("/admin/api/ship/flag", headers=self.h, json={"on": True})

    def test_off_by_default(self):
        self.assertFalse(self.c.get("/admin/api/ship/flag", headers=self.h).get_json()["enabled"])
        self.assertEqual(self.ship().status_code, 403)
        self.assertEqual(self.ms.calls, [])

    def test_ship_from_template(self):
        self.on()
        r = self.ship().get_json()["op"]
        self.assertEqual((r["status"], r["msNumber"], r["number"]), ("sent", "00001", "00077"))
        doc = self.ms.demands[0]
        self.assertEqual(doc["externalCode"], r["id"])
        self.assertTrue(doc["applicable"])
        self.assertEqual(doc["positions"], [{"quantity": 2, "price": 150000}])     # позиции — из шаблона по заказу
        self.assertIn("Отгружено из системы AMURA", doc["description"])
        self.assertEqual(self.ship().status_code, 400)                              # второй раз — нет
        self.assertEqual(len(self.ms.demands), 1)
        self.assertNotIn("PUT", [c[0] for c in self.ms.calls if "customerorder" in c[1]])   # статус заказа не трогаем

    def test_scenario_already_shipped(self):
        self.on()
        self.ms.order_demands = ["scenario1"]
        r = self.ship()
        self.assertEqual(r.status_code, 400)
        self.assertIn("уже есть отгрузка", r.get_json()["error"])
        self.assertEqual(self.ms.demands, [])

    def test_lost_response_no_duplicate(self):
        self.on()
        self.ms.fail = ["lost"]
        r = self.ship().get_json()["op"]
        self.assertEqual(r["status"], "queued")
        self.assertEqual(self.ship().get_json()["op"]["id"], r["id"])               # в очереди — новую не создаём
        r = shipments.send(r["id"])
        self.assertEqual((r["status"], len(self.ms.demands)), ("sent", 1))         # нашли по externalCode

    def test_scenario_created_while_queued(self):
        self.on()
        self.ms.fail = [requests.ConnectionError("timeout")]
        r = self.ship().get_json()["op"]
        self.ms.order_demands = ["scenario1"]                                       # пока ждали — отгрузил сценарий
        r = shipments.send(r["id"])
        self.assertEqual(r["status"], "error")
        self.assertEqual(self.ms.demands, [])

    def test_flag_owner_only(self):
        self.assertEqual(self.c.post("/admin/api/ship/flag", json={"on": True}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
