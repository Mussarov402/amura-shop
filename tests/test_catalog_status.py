"""Подсказка «Есть на складе, но не на сайте»: только товары, которые не выйдут сами, с причиной."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import catalog_status as cs  # noqa: E402

R, O = "Розничная цена", "Оптовая цена"
P = lambda pid, code="", archived=0, prices=None: {"id": pid, "code": code, "name": "Товар " + pid, "archived": archived,
                                                    "prices": json.dumps(prices if prices is not None else {R: 1500, O: 1000})}
PRODUCTS = [P("onsite"), P("soon"), P("noprice", prices={}), P("arch", archived=1), P("hid"), P("load", code="00308"), P("nostock")]
STOCK = {"onsite": 4, "soon": 6, "noprice": 2, "arch": 1, "hid": 2, "load": 99}
LIVE = {"items": [{"id": "onsite", "img": "img/onsite.webp"}], "hiddenItems": [{"id": "hid", "img": "https://api/pimg/hid"}], "outItems": []}


class CatalogStatus(unittest.TestCase):
    def test_pending(self):
        rows = cs.pending(PRODUCTS, STOCK, {"hid"}, LIVE)
        self.assertEqual({x["id"]: x["note"] for x in rows}, {"noprice": "нет цены", "arch": "в архиве", "hid": "скрыт в витрине"})
        self.assertEqual(next(x for x in rows if x["id"] == "hid")["img"], "https://api/pimg/hid")   # фото с сервера — как есть

    def test_no_publish_button(self):
        import app
        rules = {r.rule for r in app.app.url_map.iter_rules()}
        self.assertNotIn("/admin/api/catalog/publish", rules)                # выгрузку вручную убрали — сайт обновляется сам
        self.assertIn("/admin/api/not-on-site", rules)


if __name__ == "__main__":
    unittest.main()
