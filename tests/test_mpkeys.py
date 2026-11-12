"""«Настройки → Ключи площадок» (05.11.2026): ключи в базе зашифрованы, важнее app.env, в ответах — маска,
аудит без значения, «Проверить» для Ozon/WB, только владелец."""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask  # noqa: E402

import admin  # noqa: E402
import marketplaces as mpk  # noqa: E402
import modules  # noqa: E402
import mpkeys  # noqa: E402
import order_hook as oh  # noqa: E402

KEY = "ozon-api-key-VERYSECRET-9f3a"


class Keys(unittest.TestCase):
    def setUp(self):
        self.patch(oh, "ORDER_SECRET", b"test-secret")
        self.env = {k: os.environ.get(k) for k in mpkeys.NAMES}
        self.addCleanup(self.restore)
        with mpkeys.db() as d:
            d.run("DELETE FROM setting WHERE key LIKE 'envkey:%%'")
            d.run("DELETE FROM key_log")
        self.patch(mpkeys, "_orig", {k: None for k in mpkeys.NAMES} | {"OZON_CLIENT_ID": "111", "OZON_API_KEY": "server-key-0000"})
        mpkeys.apply()
        self.role = "owner"
        self.patch(admin, "who", lambda: {"role": self.role, "perms": [], "name": "Владелец"})
        app = Flask(__name__)
        app.register_blueprint(mpkeys.bp)
        self.c = app.test_client()

    def restore(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def test_db_key_wins_masked_audited_and_clear_restores(self):
        self.assertEqual(os.environ["OZON_API_KEY"], "server-key-0000")          # пока в базе пусто — ключ сервера
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            r = self.c.put("/admin/api/mpkeys", json={"values": {"OZON_API_KEY": KEY, "OZON_NAME": "AMURA", "EVIL": "x", "OZON_CLIENT_ID": " "}})
        self.assertEqual(r.get_json()["saved"], ["OZON_API_KEY", "OZON_NAME"])
        self.assertEqual(os.environ["OZON_API_KEY"], KEY)                        # из базы — важнее
        self.assertEqual(os.environ["OZON_CLIENT_ID"], "111")                    # пустое поле — прежний
        self.assertNotIn("EVIL", os.environ)
        with mpkeys.db() as d:
            raw = d.run("SELECT value FROM setting WHERE key='envkey:OZON_API_KEY'", one=True)[0]
        self.assertNotIn(KEY, raw)
        j = self.c.get("/admin/api/mpkeys").get_json()
        body = json.dumps(j, ensure_ascii=False)
        self.assertNotIn(KEY, body + out.getvalue())
        k = {x["name"]: x for g in j["groups"] for x in g["keys"]}
        self.assertEqual((k["OZON_API_KEY"]["value"], k["OZON_API_KEY"]["from"]), ("••••9f3a", "система AMURA"))
        self.assertEqual((k["OZON_NAME"]["value"], k["OZON_CLIENT_ID"]["from"]), ("AMURA", "сервер"))   # название — не секрет
        self.assertEqual({(l["name"], l["action"], l["who"]) for l in j["log"]},
                         {("OZON_API_KEY", "изменён", "Владелец"), ("OZON_NAME", "изменён", "Владелец")})   # аудит без значения
        self.c.put("/admin/api/mpkeys", json={"clear": ["OZON_API_KEY"]})
        self.assertEqual(os.environ["OZON_API_KEY"], "server-key-0000")          # убрали — снова ключ сервера
        self.assertIn("убран", self.c.get("/admin/api/mpkeys").get_json()["log"][0]["action"])

    def test_owner_only(self):
        self.role = "staff"
        self.assertEqual(self.c.get("/admin/api/mpkeys").status_code, 403)
        self.assertEqual(self.c.put("/admin/api/mpkeys", json={"values": {"WB_API_TOKEN": "x" * 20}}).status_code, 403)

    def test_check_ozon_and_wb(self):
        self.c.put("/admin/api/mpkeys", json={"values": {"OZON_CLIENT_ID": "111", "OZON_API_KEY": KEY, "WB_API_TOKEN": "wb-token-123456789"}})
        mpk.channels()

        def post(url, json=None, headers=None, timeout=None):
            assert headers["Api-Key"] == KEY
            return type("R", (), {"status_code": 200, "json": lambda s: {"company": {"name": "AMURA", "currency": "KZT", "country": "KZ"}},
                                  "text": ""})()

        def req(method, url, headers=None, timeout=None, **k):
            return type("R", (), {"status_code": 200, "json": lambda s: {"name": "ИП", "tradeMark": "AMURA WB"}, "text": ""})()
        with mock.patch.object(mpk.requests, "post", post), mock.patch.object(mpk.requests, "request", req):
            self.assertEqual(self.c.post("/admin/api/mpkeys/check/ozon").get_json(), {"ok": True, "name": "AMURA", "currency": "KZT", "country": "KZ"})
            self.assertEqual(self.c.post("/admin/api/mpkeys/check/wb").get_json()["name"], "AMURA WB")
        bad = lambda *a, **k: type("R", (), {"status_code": 403, "json": lambda s: {"message": "Invalid Api-Key"}, "text": ""})()
        with mock.patch.object(mpk.requests, "post", bad):
            r = self.c.post("/admin/api/mpkeys/check/ozon")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn(KEY, r.get_data(as_text=True))

    def test_freedom_keys_flag_and_check(self):
        import freedom
        r = self.c.put("/admin/api/mpkeys", json={"values": {"FREEDOM_TEST": "да"}})
        self.assertEqual(r.status_code, 400)                                     # переключатель — только 0 / 1
        self.c.put("/admin/api/mpkeys", json={"values": {"FREEDOM_MERCHANT_ID": "552233", "FREEDOM_SECRET_KEY": "fp-secret-ABCDEFGH", "FREEDOM_TEST": "1"}})
        self.assertTrue(freedom.keys_set() and freedom.testing())
        k = {x["name"]: x for g in self.c.get("/admin/api/mpkeys").get_json()["groups"] for x in g["keys"]}
        self.assertEqual((k["FREEDOM_SECRET_KEY"]["value"], k["FREEDOM_TEST"]["flag"], k["FREEDOM_TEST"]["value"]), ("••••EFGH", True, "1"))
        sent = []

        def post(url, data=None, timeout=None):
            sent.append((url, data))
            ok = data["pg_sig"] == freedom.sign("get_status3.php", {k: v for k, v in data.items() if k != "pg_sig"}, "fp-secret-ABCDEFGH")
            body = ("<response><pg_status>error</pg_status><pg_error_code>340</pg_error_code><pg_error_description>Платеж не найден</pg_error_description></response>"
                    if ok else "<response><pg_status>error</pg_status><pg_error_code>9998</pg_error_code><pg_error_description>Неверная подпись запроса</pg_error_description></response>")
            return type("R", (), {"status_code": 200, "text": body})()
        with mock.patch.object(freedom.requests, "post", post):
            j = self.c.post("/admin/api/mpkeys/check/freedom").get_json()
            self.assertTrue(j["ok"], j)
            self.assertIn("тестовый режим", j["name"])
            self.assertTrue(sent[-1][0].endswith("/get_status3.php") and "pg_amount" not in sent[-1][1])   # без суммы — ничего не списывается
            self.c.put("/admin/api/mpkeys", json={"values": {"FREEDOM_SECRET_KEY": "wrong-secret-0000"}})
            r = self.c.post("/admin/api/mpkeys/check/freedom")
        self.assertEqual(r.status_code, 502)
        self.assertIn("Неверная подпись", r.get_json()["error"])
        self.assertNotIn("wrong-secret", r.get_data(as_text=True))

    def test_apipay_retail_key_and_check(self):
        import apipay
        self.c.put("/admin/api/mpkeys", json={"values": {"APIPAY_API_KEY": "apipay-key-SECRET-77ab"}})
        self.assertEqual(apipay.key(), "apipay-key-SECRET-77ab")                    # ключ из базы — у модуля оплаты
        k = {x["name"]: x for g in self.c.get("/admin/api/mpkeys").get_json()["groups"] for x in g["keys"]}
        self.assertEqual(k["APIPAY_API_KEY"]["value"], "••••77ab")
        resp = lambda code, body: type("R", (), {"status_code": code, "json": lambda s: body})()
        with mock.patch.object(apipay.requests, "get", lambda *a, **kw: resp(404, {"message": "not found", "is_sandbox": True})):
            j = self.c.post("/admin/api/mpkeys/check/apipay").get_json()
        self.assertEqual((j["ok"], j["name"]), (True, "ключ принят · песочница"))
        with mock.patch.object(apipay.requests, "get", lambda *a, **kw: resp(401, {})):
            r = self.c.post("/admin/api/mpkeys/check/apipay")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("SECRET", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
