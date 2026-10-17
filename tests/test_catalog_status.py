"""«Оприходовано, ещё не на сайте»: остаток есть, а в catalog.json нет (или там 0); пометка «не выйдет: …» только по фильтрам выгрузки;
«Выгрузить сейчас» — ключ, раз в 2 минуты, вызов GitHub, только владелец."""
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
import catalog_status as cs  # noqa: E402

R, O = "Розничная цена", "Оптовая цена"
P = lambda pid, code="", archived=0, prices=None: {"id": pid, "code": code, "name": "Товар " + pid, "archived": archived,
                                                    "prices": json.dumps(prices if prices is not None else {R: 1500, O: 1000})}
PRODUCTS = [P("onsite"), P("new"), P("zero_in_cat"), P("noprice", prices={}), P("arch", archived=1), P("hid"), P("load", code="00308"),
            P("nostock")]
EXPORTED = {"onsite": 4, "zero_in_cat": 0}
STOCK = {"onsite": 4, "new": 6, "zero_in_cat": 3, "noprice": 2, "arch": 1, "hid": 2, "load": 99}
LIVE = {"items": [{"id": "onsite", "img": "img/onsite.webp"}], "hiddenItems": [], "outItems": [{"id": "zero_in_cat", "img": "img/z.webp"}]}


class Resp:
    def __init__(self, code, data=None):
        self.status_code, self._d, self.text = code, data or {}, ""
        self.ok = code < 400

    def json(self):
        return self._d


class CatalogStatus(unittest.TestCase):
    def test_pending(self):
        rows = cs.pending(PRODUCTS, EXPORTED, STOCK, {"hid"}, LIVE)
        got = {x["id"]: x["note"] for x in rows}
        self.assertEqual(got, {"new": "", "zero_in_cat": "", "noprice": "нет цены", "arch": "в архиве", "hid": "скрыт в витрине"})
        self.assertEqual([x["id"] for x in rows][:2], ["new", "zero_in_cat"])          # сначала те, что выйдут после выгрузки
        self.assertTrue(next(x for x in rows if x["id"] == "zero_in_cat")["img"].endswith("/img/z.webp"))

    def test_publish(self):
        old = admin.who
        admin.who = lambda: {"role": "owner", "perms": [], "name": "Владелец"}
        self.addCleanup(lambda: setattr(admin, "who", old))
        app = Flask(__name__)
        app.register_blueprint(cs.bp)
        c = app.test_client()
        os.environ.pop("GH_ACTIONS_TOKEN", None)
        self.assertEqual(c.post("/admin/api/catalog/publish").status_code, 400)          # без ключа — нельзя
        self.assertFalse(c.get("/admin/api/catalog/publish").get_json()["key"])
        os.environ["GH_ACTIONS_TOKEN"] = "t"
        self.addCleanup(lambda: os.environ.pop("GH_ACTIONS_TOKEN", None))
        calls = []

        def req(method, url, **kw):
            calls.append((method, url, kw.get("json")))
            if method == "POST":
                return Resp(204)
            return Resp(200, {"workflow_runs": [{"status": "in_progress", "event": "workflow_dispatch", "created_at": "2026-10-17T08:00:00Z"}]})
        old_req = cs.requests.request
        cs.requests.request = req
        self.addCleanup(lambda: setattr(cs.requests, "request", old_req))
        cs._last_dispatch[0] = 0
        r = c.post("/admin/api/catalog/publish").get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(calls[0], ("POST", "https://api.github.com/repos/Mussarov402/amura-shop/actions/workflows/catalog.yml/dispatches", {"ref": "main"}))
        self.assertEqual(c.post("/admin/api/catalog/publish").status_code, 429)          # второй раз сразу — нет
        st = c.get("/admin/api/catalog/publish").get_json()
        self.assertEqual((st["run"]["status"], st["run"]["at"], st["run"]["manual"]), ("running", "17.10 13:00", True))
        admin.who = lambda: {"role": "staff", "perms": ["products"], "name": "Сотрудник"}
        self.assertEqual(c.post("/admin/api/catalog/publish").status_code, 403)          # только владелец


if __name__ == "__main__":
    unittest.main()
