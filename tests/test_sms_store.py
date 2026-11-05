"""Вход по SMS (05.11.2026): коды и счётчики — в базе, а не в памяти процесса; отказ Mobizon — в журнал без номера."""
import contextlib
import hashlib
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask  # noqa: E402

import inbox  # noqa: E402
import order_hook as oh  # noqa: E402

PHONE = "77019998877"


class SmsStore(unittest.TestCase):
    def setUp(self):
        with inbox.db() as d:
            d.run("DELETE FROM setting WHERE key LIKE 'smscode:%%' OR key='smsday'")
        self.sms = []
        p = [mock.patch.object(oh, "MOBIZON_KEY", "k"), mock.patch.object(oh, "too_many", lambda *a, **k: False),
             mock.patch.object(oh, "client_ip", lambda: "127.0.0.1"),
             mock.patch.object(oh, "send_sms", lambda ph, text: self.sms.append((ph, text))),
             mock.patch.object(oh, "login_phone", lambda phone, cid=None: ({"id": "cid-1"}, False)),
             mock.patch.object(oh, "profile", lambda cp: {"city": "Алматы"}), mock.patch.object(oh, "tg", lambda *a, **k: {})]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        self.c = app.test_client()

    def send(self):
        return self.c.post("/auth/sms/send", json={"phone": "+7 701 999 88 77"})

    def test_code_survives_restart(self):
        self.assertEqual(self.send().status_code, 200)
        code = "".join(ch for ch in self.sms[-1][1] if ch.isdigit())[-6:]
        oh._codes = oh._SmsStore()            # «перезапуск»: новый объект, данные только в базе
        with inbox.db() as d:
            self.assertTrue(inbox.get_setting(d, "smscode:" + PHONE))
            self.assertNotIn(code, inbox.get_setting(d, "smscode:" + PHONE))   # в базе только хэш
        r = self.c.post("/auth/sms/verify", json={"phone": PHONE, "code": "000000" if code != "000000" else "111111"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/auth/sms/verify", json={"phone": PHONE, "code": code})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["token"])
        r = self.c.post("/auth/sms/verify", json={"phone": PHONE, "code": code})   # код одноразовый
        self.assertEqual(r.status_code, 400)

    def test_tries_saved(self):
        oh._codes[PHONE] = {"h": hashlib.sha256(b"123456" + PHONE.encode()).hexdigest(), "t": time.time(), "tries": 0}
        for _ in range(5):
            self.assertEqual(self.c.post("/auth/sms/verify", json={"phone": PHONE, "code": "999999"}).status_code, 400)
        self.assertEqual(self.c.post("/auth/sms/verify", json={"phone": PHONE, "code": "123456"}).status_code, 429)

    def test_limits_in_db(self):
        self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.send().status_code, 429)          # минута между кодами
        v = oh._codes.get(PHONE)
        v["t"] -= 120
        v["sent"] = [time.time() - 100, time.time() - 50, v["sent"][0]]
        oh._codes[PHONE] = v
        self.assertEqual(self.send().status_code, 429)          # 3 кода в час на номер
        self.assertEqual(oh._sms_day_add(), 1)                  # дневной счётчик — в базе
        with mock.patch.object(oh, "SMS_DAILY_LIMIT", 1):
            with inbox.db() as d:
                d.run("DELETE FROM setting WHERE key LIKE 'smscode:%%'")
            self.assertEqual(self.send().status_code, 503)

    def test_refusal_logged_without_number(self):
        def bad(ph, text):
            raise RuntimeError("Недостаточно средств на балансе")
        out = io.StringIO()
        with mock.patch.object(oh, "send_sms", bad), contextlib.redirect_stdout(out):
            r = self.send()
        self.assertEqual(r.status_code, 502)
        log = out.getvalue()
        self.assertIn("SMS не отправлено (…8877): Недостаточно средств на балансе", log)
        self.assertNotIn(PHONE, log)
        self.assertIsNone(oh._codes.get(PHONE))                 # кода нет — отправки не было


if __name__ == "__main__":
    unittest.main()
