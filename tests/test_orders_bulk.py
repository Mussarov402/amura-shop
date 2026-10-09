"""Массово по заказам: смена статуса и удаление (только владелец; с едущим курьером — нельзя)."""
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
import courier  # noqa: E402
import order_hook as oh  # noqa: E402

ID = "1" * 8 + "-1111-1111-1111-" + "1" * 12


class Bulk(unittest.TestCase):
    def setUp(self):
        self.calls, self.role = [], "owner"

        def ms(m, p, **kw):
            self.calls.append((m, p, kw.get("json")))
            if p == "/entity/customerorder/metadata":
                return {"states": [{"name": n, "meta": {"href": "st/" + n}} for n in ("Новый", "Собран", "Отменен")]}
            if p == "/entity/customerorder" and m == "GET":
                return {"rows": [{"id": "2" * 8 + "-2222-2222-2222-" + "2" * 12}]}
            return {}
        self.patch(oh, "ms", ms)
        self.patch(admin, "who", lambda: {"role": self.role, "perms": ["orders"], "name": "Т"})
        self.patch(admin, "_olog", lambda *a, **k: None)
        self.patch(courier, "auto_express", lambda *a, **k: "off")
        oh._cache.pop("adm_order_states", None)
        with courier.db() as d:
            d.run("DELETE FROM ya_claim")
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def post(self, body):
        r = self.c.post("/admin/api/orders/bulk", data=json.dumps(body), content_type="application/json")
        return r.status_code, json.loads(r.data)

    def test_state(self):
        st, r = self.post({"action": "state", "state": "Отменен", "orders": [{"number": "1542", "id": ID}, {"number": "1543"}]})
        self.assertEqual(r["result"], {"1542": "ok", "1543": "ok"})
        puts = [c for c in self.calls if c[0] == "PUT"]
        self.assertEqual([p[2]["state"]["meta"]["href"] for p in puts], ["st/Отменен", "st/Отменен"])
        self.assertEqual(self.post({"action": "state", "state": "Нет такого", "orders": [{"number": "1"}]})[0], 400)

    def test_delete(self):
        with courier.db() as d:
            courier._save(d, "1544", claim_id="c1", status="pickuped")       # курьер уже везёт — не удаляем
        st, r = self.post({"action": "delete", "orders": [{"number": "1542", "id": ID}, {"number": "1544", "id": ID}]})
        self.assertEqual(r["result"]["1542"], "ok")
        self.assertIn("курьер", r["result"]["1544"])
        self.assertEqual([c[1] for c in self.calls if c[0] == "DELETE"], [f"/entity/customerorder/{ID}"])
        self.role = "staff"
        self.assertEqual(self.post({"action": "delete", "orders": [{"number": "1542", "id": ID}]})[0], 403)


if __name__ == "__main__":
    unittest.main()
