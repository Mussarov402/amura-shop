"""Сервер на Hoster.kz: hello — в журнал; отчёты — только с подписью закреплённого ключа; alert — владельцу."""
import base64
import json
import os
import sys
import tempfile
import time
import unittest

os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

import ops  # noqa: E402
import order_hook as oh  # noqa: E402
from app import app  # noqa: E402


class Ops(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.environ["AMURA_KEYS"] = self.d
        import importlib
        import report
        self.r = importlib.reload(report)
        self.r.keygen()
        self.c = app.test_client()
        self.sent = []
        for obj, name, val in ((oh, "too_many", lambda *a, **k: False), (oh, "OWNER", "1"),
                               (oh, "notify_bg", lambda fn: fn()), (oh, "tg", lambda m, **k: self.sent.append(k["text"]))):
            old = getattr(obj, name)
            setattr(obj, name, val)
            self.addCleanup(setattr, obj, name, old)
        ops._seen.clear()

    def pin(self, pub):
        f = os.path.join(self.d, "server-key.json")
        with open(f, "w") as fh:
            json.dump({**pub, "fpr": self.r.fpr(pub)}, fh)
        old = ops.KEY_FILE
        ops.KEY_FILE = f
        self.addCleanup(setattr, ops, "KEY_FILE", old)

    def post(self, body, key=None):
        data = json.dumps(body).encode()
        sig = base64.b64encode((key or self.r._load("sign.key", Ed25519PrivateKey)).sign(data)).decode()
        return self.c.post("/ops/report", data=data, headers={"X-Amura-Sig": sig, "Content-Type": "application/json"})

    def test_flow(self):
        pub = self.r.public()
        j = self.c.post("/ops/hello", json={**pub, "fpr": self.r.fpr(pub)}).get_json()
        self.assertEqual(j, {"ok": True, "pinned": False})
        body = {"level": "alert", "text": "диск заполнен", "t": int(time.time())}
        self.assertEqual(self.post(body).status_code, 403)                      # ключ не закреплён
        self.pin(pub)
        self.assertTrue(self.c.post("/ops/hello", json={**pub, "fpr": self.r.fpr(pub)}).get_json()["pinned"])
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("🚨", self.sent[0])
        self.assertEqual(self.post(body).status_code, 200)
        self.assertEqual(len(self.sent), 1)                                      # повтор тревоги — не чаще раза в час
        self.assertEqual(self.post(body, key=Ed25519PrivateKey.generate()).status_code, 403)   # чужая подпись
        self.assertEqual(self.post({**body, "t": 1}).status_code, 400)           # старый отчёт

    def test_env_sealed_for_server_only(self):
        """Переменные — только по подписи закреплённого ключа и только зашифрованными для box этого сервера."""
        pub = self.r.public()
        self.pin(pub)
        os.environ["AMURA_TEST_SECRET"] = "s3cr\"et"
        os.environ["DATABASE_URL"] = "postgres://render/db"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("AMURA_TEST_SECRET", "DATABASE_URL")])
        import unittest.mock as um
        def post(path, body, sign=False):
            data = json.dumps(body).encode()
            hdr = {"X-Amura-Sig": base64.b64encode(self.r._load("sign.key", Ed25519PrivateKey).sign(data)).decode()} if sign else {}
            r = self.c.post(path, data=data, headers={**hdr, "Content-Type": "application/json"})
            self.assertEqual(r.status_code, 200, r.get_json())
            j = r.get_json()
            self.assertNotIn("s3cr", json.dumps(j))                               # в ответе — только шифр
            return j
        f = os.path.join(self.d, "app.env")
        with um.patch.object(self.r, "_post", post):
            self.r.env(f)
        with open(f) as fh:
            txt = fh.read()
        self.assertIn('AMURA_TEST_SECRET="s3cr\\"et"', txt)
        self.assertIn('RENDER_DATABASE_URL="postgres://render/db"', txt)
        self.assertNotIn("\nDATABASE_URL=", "\n" + txt)
        self.assertEqual(os.stat(f).st_mode & 0o777, 0o600)
        data = json.dumps({"t": int(time.time())}).encode()                      # чужая подпись — отказ
        sig = base64.b64encode(Ed25519PrivateKey.generate().sign(data)).decode()
        self.assertEqual(self.c.post("/ops/env", data=data, headers={"X-Amura-Sig": sig}).status_code, 403)

    def test_keys_stay_and_secret_parts_private(self):
        before = self.r.public()
        self.r.keygen()                                                          # повторный запуск — те же ключи
        self.assertEqual(self.r.public(), before)
        for n in ("sign.key", "box.key"):
            self.assertEqual(os.stat(os.path.join(self.d, n)).st_mode & 0o777, 0o600)

    def test_pinned_file_in_repo_is_valid_json(self):
        with open(os.path.join(os.path.dirname(ops.KEY_FILE), "server-key.json")) as f:
            json.load(f)




class Standby(unittest.TestCase):
    """Запасной сервер (AMURA_STANDBY=1): фон не запускается — ни через bg.start, ни потоками при импорте модулей."""
    def test_no_background(self):
        import threading
        import bg
        ran = []
        old = bg.STANDBY
        bg.STANDBY = True
        self.addCleanup(lambda: setattr(bg, "STANDBY", old))
        bg.start(lambda: ran.append("bg"))
        with bg.standby_import():
            threading.Thread(target=lambda: ran.append("thread"), daemon=True).start()
        t = threading.Thread(target=lambda: ran.append("after"))
        t.start()
        t.join()
        self.assertEqual(ran, ["after"])                                         # после импорта потоки снова работают (gunicorn)


class Migration(unittest.TestCase):
    """Ночь переезда: Render «только чтение», проверка сертификатов пересылается на сервер, режимы не утекают в /ops/env."""
    def setUp(self):
        self.c = app.test_client()

    def test_readonly(self):
        import app as appmod
        from unittest import mock
        with mock.patch.object(appmod, "READONLY", True):
            r = self.c.post("/order", json={})
            self.assertEqual((r.status_code, r.headers.get("Retry-After")), (503, "600"))
            self.assertIn("переезжает", r.get_json()["error"])
            self.assertNotEqual(self.c.post("/ops/hello", json={}).status_code, 503)    # отчёты сервера идут
            self.assertEqual(self.c.get("/").status_code, 200)                           # чтение работает
        self.assertNotEqual(self.c.post("/order", json={}).status_code, 503)

    def test_acme_forward_only_on_render(self):
        from unittest import mock
        got = {}

        def fake_get(url, **kw):
            got.update(url=url, host=kw["headers"]["Host"])
            return mock.Mock(status_code=200, content=b"tok.key")
        tok = "A" * 43
        with mock.patch("requests.get", side_effect=fake_get):
            self.assertEqual(self.c.get(f"/.well-known/acme-challenge/{tok}").status_code, 404)   # не Render — нет
            with mock.patch.dict(os.environ, {"RENDER": "true"}):
                for host in ("amura.kz", "www.amura.kz", "opt.amura.kz", "api.amura.kz"):
                    r = self.c.get(f"/.well-known/acme-challenge/{tok}", headers={"Host": host})
                    self.assertEqual((r.status_code, r.data), (200, b"tok.key"), host)
                    self.assertEqual(got, {"url": f"http://{ops.SERVER_IP}/.well-known/acme-challenge/{tok}", "host": host})
                self.assertEqual(self.c.get("/.well-known/acme-challenge/..%2Fx").status_code, 404)

    def test_modes_not_sent_to_server(self):
        self.assertIn("AMURA_STANDBY", ops.SKIP_ENV)
        self.assertIn("AMURA_READONLY", ops.SKIP_ENV)


class ReadonlyForward(unittest.TestCase):
    """Render после смены DNS (AMURA_READONLY=1 + AMURA_FORWARD_TO): записи пересылаются на новый сервер, а не 503."""
    def test_forward(self):
        import app as appmod
        from unittest import mock
        seen = {}

        class Pool:
            def __init__(self, ip, port, **kw):
                seen.update(ip=ip, sni=kw["server_hostname"])

            def request(self, method, path, body=None, headers=None, redirect=False):
                seen.update(method=method, path=path, body=body, host=headers["Host"])
                return mock.Mock(status=200, data=b'{"ok":true}', headers={"Content-Type": "application/json"})
        c = app.test_client()
        with mock.patch.object(appmod, "READONLY", True), mock.patch.object(appmod, "FORWARD_TO", "89.126.206.3"), \
                mock.patch("urllib3.HTTPSConnectionPool", Pool):
            r = c.post("/tg/SECRET?x=1", data=b'{"update_id":1}', headers={"Host": "api.amura.kz", "Content-Type": "application/json"})
            self.assertEqual((r.status_code, r.get_json()), (200, {"ok": True}))
            self.assertEqual(seen, {"ip": "89.126.206.3", "sni": "api.amura.kz", "method": "POST", "path": "/tg/SECRET?x=1",
                                    "body": b'{"update_id":1}', "host": "api.amura.kz"})
            c.post("/order", json={}, headers={"Host": "api.amura.kz"})
            self.assertEqual(seen["sni"], "api.amura.kz")
        with mock.patch.object(appmod, "READONLY", True), mock.patch.object(appmod, "FORWARD_TO", "89.126.206.3"), \
                mock.patch("urllib3.HTTPSConnectionPool", side_effect=OSError("down")):
            self.assertEqual(c.post("/order", json={}).status_code, 503)               # сервер не ответил — по-старому 503


    def test_onrender_redirects_to_admin(self):
        """Приложение, установленное со старого адреса *.onrender.com: GET/HEAD → 301, записи → 308 на admin.amura.kz."""
        import app as appmod
        from unittest import mock
        c = app.test_client()
        h = {"Host": "amura-shop-api.onrender.com"}
        with mock.patch.object(appmod, "READONLY", True):
            r = c.get("/admin?x=1&y=2", headers=h)
            self.assertEqual((r.status_code, r.headers["Location"]), (301, "https://admin.amura.kz/admin?x=1&y=2"))
            self.assertEqual(c.head("/admin", headers=h).status_code, 301)
            for m in ("post", "put", "patch", "delete"):
                r = getattr(c, m)("/admin/api/orders/1907", json={}, headers=h)
                self.assertEqual((r.status_code, r.headers["Location"]), (308, "https://admin.amura.kz/admin/api/orders/1907"), m)
            self.assertNotEqual(c.post("/ops/hello", json={}, headers=h).status_code, 308)     # отчёты сервера — как есть
            self.assertNotIn(c.options("/order", headers=h).status_code, (301, 308))         # предзапрос CORS не перенаправляем
            self.assertNotIn(c.get("/", headers={"Host": "api.amura.kz"}).status_code, (301, 308))   # наши домены — без перенаправления
        self.assertNotIn(c.get("/admin", headers=h).status_code, (301, 308))                 # без READONLY (сервер) — как было


if __name__ == "__main__":
    unittest.main()
