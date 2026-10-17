"""Заявки на сотрудничество: проверка полей и согласия, ловушка для ботов, лимит, список и статус в системе AMURA, модуль выключен — 404."""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402
import admin  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402
import partners  # noqa: E402

OK = {"kind": "opt", "name": "Динара", "phone": "+7 701 555 44 33", "city": "Шымкент", "shop": "@beauty_shym", "consent": True, "site": "rtl"}


class Partners(unittest.TestCase):
    def setUp(self):
        with partners.db() as d:
            d.run("DELETE FROM partner_req")
        modules.update("partners", on=True)
        for obj, name, val in ((oh, "too_many", lambda *a, **k: False), (oh, "opt_min", lambda: 20000),
                               (admin, "who", lambda: {"role": "owner", "perms": [], "name": "Владелец"})):
            old = getattr(obj, name)
            setattr(obj, name, val)
            self.addCleanup(lambda o=obj, n=name, v=old: setattr(o, n, v))
        import push
        old = push.notify
        push.notify = lambda *a, **k: self.pushed.append(a)
        self.addCleanup(lambda: setattr(push, "notify", old))
        self.pushed = []
        app = Flask(__name__)
        app.register_blueprint(partners.bp)
        self.c = app.test_client()

    def post(self, **kw):
        return self.c.post("/partners/apply", json=OK | kw)

    def test_apply_and_admin(self):
        self.assertEqual(self.c.get("/partners/info").get_json()["optMin"], 20000)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post(kind="seller", what="").status_code, 400)                        # продавцу — что продаёт
        self.assertEqual(self.post(kind="seller", what="Уход за волосами", legal="ИП").status_code, 200)
        self.assertEqual(self.post(consent=False).status_code, 400)                                 # без согласия — нет
        self.assertEqual(self.post(phone="123").status_code, 400)
        self.assertEqual(self.post(kind="boss").status_code, 400)
        self.assertEqual(self.post(website="http://spam").status_code, 200)                         # ловушка: «ок», но не сохраняем
        self.assertEqual(len(self.pushed), 2)
        r = self.c.get("/admin/api/partners").get_json()
        self.assertEqual(len(r["items"]), 2)
        s = next(x for x in r["items"] if x["kind"] == "seller")
        self.assertEqual((s["phone"], s["legal"], s["status"], s["kindName"]), ("77015554433", "ИП", "new", "Стать продавцом"))
        self.assertEqual(self.c.post(f"/admin/api/partners/{s['id']}", json={"status": "contacted"}).status_code, 200)
        self.assertEqual(self.c.get("/admin/api/partners").get_json()["counts"], {"new": 1, "contacted": 1, "rejected": 0})
        self.assertEqual(self.c.post(f"/admin/api/partners/{s['id']}", json={"status": "x"}).status_code, 400)

    def test_cors_for_sites(self):
        """Сайты amura.kz / opt.amura.kz — другой адрес: без CORS браузер пишет «Нет связи с сервером»."""
        import app
        c = app.app.test_client()
        for origin in ("https://amura.kz", "https://opt.amura.kz"):
            h = {"Origin": origin}
            self.assertEqual(c.get("/partners/info", headers=h).headers.get("Access-Control-Allow-Origin"), origin)
            self.assertEqual(c.options("/partners/apply", headers=h).headers.get("Access-Control-Allow-Origin"), origin)
            self.assertEqual(c.options("/an/ev", headers=h).headers.get("Access-Control-Allow-Origin"), origin)
        self.assertIsNone(self.c.get("/partners/info", headers={"Origin": "https://evil.example"}).headers.get("Access-Control-Allow-Origin"))

    def test_limit_and_off(self):
        oh.too_many = lambda *a, **k: True
        self.assertEqual(self.post().status_code, 429)
        modules.update("partners", on=False)
        self.addCleanup(lambda: modules.update("partners", on=True))
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(self.c.get("/admin/api/partners").status_code, 404)


if __name__ == "__main__":
    unittest.main()
