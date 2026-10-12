"""Контрагенты: создание и правка карточки с двойной записью в МойСклад — без дублей, тип цены только у владельца."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401
import admin  # noqa: E402
import app  # noqa: E402
import contacts  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402
import requests  # noqa: E402


class FakeCpMS:
    def __init__(self):
        self.cps, self.calls, self.fail = {}, [], []

    def __call__(self, method, path, params=None, json=None, **kw):
        self.calls.append((method, path, params, json))
        if path == "/context/companysettings/pricetype":
            return [{"id": "pt1", "name": "Оптовая цена"}, {"id": "pt2", "name": "Розничная цена"}]
        if path == "/entity/counterparty" and method == "GET":
            code = params["filter"].split("externalCode=")[-1]
            return {"rows": [c for c in self.cps.values() if c.get("externalCode") == code]}
        if path == "/entity/counterparty" and method == "POST":
            c = {"id": f"cp{len(self.cps) + 1}", "updated": "2026-10-12 10:00:00.000", **json}
            if self.fail:
                e = self.fail.pop(0)
                if e == "lost":
                    self.cps[c["id"]] = c
                    raise requests.ConnectionError("timeout")
                raise e
            self.cps[c["id"]] = c
            return c
        if path.startswith("/entity/counterparty/"):
            cid = path.rsplit("/", 1)[-1]
            if method == "GET":
                return dict(self.cps[cid])
            if method == "PUT":
                self.cps[cid].update(json)
                return dict(self.cps[cid])
        raise AssertionError(f"неожиданный запрос {method} {path}")


class ContactsTest(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        self.ms = oh.ms = FakeCpMS()
        contacts._meta.clear()
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + admin.make_admin_token()}
        with mirror.db() as d:
            d.run("DELETE FROM ms_agent")
            d.run("INSERT INTO ms_agent (id, name, phone, archived, deleted) VALUES ('k1', 'Ertos', '+7 701 000 00 01', 0, 0)")
        with contacts.db() as d:
            d.run("DELETE FROM agent_q")

    def test_create(self):
        r = self.c.post("/admin/api/contacts", headers=self.h, json={"name": "Aigul Opt", "phone": "8 702 111 22 33", "city": "Шымкент",
                                                                     "tags": ["Опт", "опт", " vip "], "priceType": "pt1"}).get_json()["op"]
        self.assertEqual(r["status"], "sent")
        cp = self.ms.cps["cp1"]
        self.assertEqual((cp["name"], cp["phone"], cp["actualAddress"], cp["tags"]), ("Aigul Opt", "+77021112233", "Шымкент", ["vip", "опт"]))
        self.assertTrue(cp["priceType"]["meta"]["href"].endswith("/pricetype/pt1"))
        self.assertEqual(cp["externalCode"], r["id"])
        with mirror.db() as d:
            self.assertEqual(d.run("SELECT name, phone FROM ms_agent WHERE id='cp1'", one=True), ("Aigul Opt", "+77021112233"))   # сразу в нашей копии

    def test_duplicate_phone_and_validation(self):
        r = self.c.post("/admin/api/contacts", headers=self.h, json={"name": "Другой", "phone": "+7 (701) 000-00-01"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Ertos", r.get_json()["error"])
        for b in ({"name": ""}, {"name": "X", "phone": "123"}, {"name": "X", "priceType": "zzz"}):
            self.assertEqual(self.c.post("/admin/api/contacts", headers=self.h, json=b).status_code, 400, b)
        self.assertEqual(self.ms.cps, {})

    def test_edit_only_changes(self):
        self.ms.cps["k1"] = {"id": "k1", "name": "Ertos", "phone": "+77010000001", "tags": ["опт"], "actualAddress": "Алматы"}
        r = self.c.put("/admin/api/contacts/k1", headers=self.h, json={"name": "Ertos", "city": "Астана", "tags": ["опт"]}).get_json()["op"]
        self.assertEqual(r["status"], "sent")
        put = [c for c in self.ms.calls if c[0] == "PUT"][0][3]
        self.assertEqual(put, {"actualAddress": "Астана"})                       # только изменённое
        self.assertEqual(self.c.put("/admin/api/contacts/nope", headers=self.h, json={"name": "A"}).status_code, 404)

    def test_staff_cannot_set_price_type(self):
        tok = admin.make_admin_token("staff", "555")
        orig = admin.who
        admin.who = lambda: {"role": "staff", "perms": ["orders"], "name": "Менеджер", "chat": "555"}
        try:
            h = {"Authorization": "Bearer " + tok}
            r = self.c.post("/admin/api/contacts", headers=h, json={"name": "Shop", "priceType": "pt1"})
            self.assertEqual(r.status_code, 403)
            r = self.c.post("/admin/api/contacts", headers=h, json={"name": "Shop", "phone": "+77003334455"})
            self.assertEqual(r.get_json()["op"]["status"], "sent")
            self.assertEqual(r.get_json()["op"]["who"], "Менеджер")
        finally:
            admin.who = orig

    def test_lost_response_no_duplicate(self):
        self.ms.fail = ["lost"]
        r = self.c.post("/admin/api/contacts", headers=self.h, json={"name": "Lost"}).get_json()["op"]
        self.assertEqual(r["status"], "queued")
        self.assertEqual(contacts.send(r["id"])["status"], "sent")
        self.assertEqual(len(self.ms.cps), 1)


if __name__ == "__main__":
    unittest.main()
