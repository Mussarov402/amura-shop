"""«Товары на сайте»: товары без остатка видны выключенными, общий переключатель скрывает или показывает все найденные."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import admin  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402

it = lambda pid, brand="Anua", qty=3: {"id": pid, "code": pid, "name": "Товар " + pid, "brand": brand, "qty": qty, "rtl": 1000, "opt": 700}


class ProductsList(unittest.TestCase):
    def setUp(self):
        self.live = {"items": [it("a"), it("b", "Celimax")], "hiddenItems": [it("c")], "outItems": [it("d", qty=0)]}
        oh._cache["live"] = (9e18, self.live)
        oh._cache.pop("gone", None)
        with mirror.db() as d:
            d.run("DELETE FROM ms_product")
            for pid, code, arch in (("a", "a", 0), ("e", "e", 0), ("f", "f", 1), ("g", oh.LOADER_CODE, 0)):
                d.run("INSERT INTO ms_product (id, kind, code, name, folder, archived, deleted, prices) VALUES (%s,'product',%s,%s,'Anua/Сыворотки',%s,0,%s)",
                      (pid, code, "Товар " + pid, arch, json.dumps({oh.PRICE_RTL: 2000, oh.PRICE_OPT: 1500})))
        self.calls = []
        self._set, self._who = admin._set_hidden, admin.who
        admin._set_hidden = lambda ids, val: self.calls.append((sorted(ids), val))
        admin.who = lambda: {"role": "owner", "perms": []}

    def tearDown(self):
        admin._set_hidden, admin.who = self._set, self._who
        oh._cache.pop("live", None)
        oh._cache.pop("gone", None)

    def get(self, qs=""):
        with app.app.test_request_context("/admin/api/products" + qs):
            return admin.products().get_json()

    def test_out_of_stock_listed_off(self):
        o = self.get()
        ids = [i["id"] for i in o["items"]]
        self.assertEqual(set(ids), {"a", "b", "c", "d", "e"})          # архивный и «грузчик» не показываем
        self.assertEqual(ids[-2:], sorted(["d", "e"]))                  # без остатка — в конце
        self.assertEqual(o["outHere"], 2)
        e = next(i for i in o["items"] if i["id"] == "e")
        self.assertTrue(e["out"])
        self.assertEqual((e["brand"], e["rtl"]), ("Anua", 2000))

    def test_all_switch(self):
        with app.app.test_request_context("/admin/api/products/hidden-all", method="POST", json={"q": "", "brand": "Anua", "hidden": True}):
            r = admin.set_all_hidden().get_json()
        self.assertEqual(self.calls, [(["a"], True)])                    # c уже скрыт, d/e без остатка — не трогаем
        self.assertEqual(r["changed"], 1)
        with app.app.test_request_context("/admin/api/products/hidden-all", method="POST", json={"hidden": False}):
            admin.set_all_hidden()
        self.assertEqual(self.calls[-1], (["c"], False))


if __name__ == "__main__":
    unittest.main()
