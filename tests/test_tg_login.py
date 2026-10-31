"""Вход через Telegram-бот (главный на обоих сайтах, слово владельца 31.10.2026): токен по nonce; в рознице с галочкой
согласия — согласие записывается клиенту «вход через Telegram»."""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask  # noqa: E402

import order_hook as oh  # noqa: E402

TOO_MANY = oh.too_many          # настоящий лимит: test_checkout / test_retail подменяют его на весь процесс


class TgLogin(unittest.TestCase):
    def setUp(self):
        self.saved, self.sent = [], []
        for name, val in {"too_many": TOO_MANY, "client_ip": lambda: "127.0.0.1", "cp_by_tg": lambda tg_id, u: {"id": "cid-7", "tags": []},
                          "profile": lambda cp: {"city": "Алматы"}, "set_tg_id": lambda cid, tg_id: None,
                          "tg": lambda method, **k: self.sent.append(k), "save_consent": lambda cid, how: self.saved.append((cid, how))}.items():
            old = getattr(oh, name)
            setattr(oh, name, val)
            self.addCleanup(setattr, oh, name, old)
        oh._hits.pop("tg:127.0.0.1", None)
        oh._tg_login.clear()
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        self.c = app.test_client()

    def login(self, consent):
        n = self.c.post("/auth/tg/start").get_json()["nonce"]
        self.assertIsNone(self.c.get("/auth/tg/poll?nonce=" + n).get_json()["token"])
        oh.handle_tg_login(n, {"id": 555, "first_name": "Аня"}, 555)
        return self.c.get(f"/auth/tg/poll?nonce={n}" + ("&consent=1" if consent else "")).get_json()

    def test_retail_consent_saved(self):
        j = self.login(True)
        self.assertTrue(j["token"])
        self.assertFalse(j["needProfile"])
        self.assertEqual(self.saved, [("cid-7", "вход через Telegram")])
        self.assertIn("вы вошли", self.sent[-1]["text"].lower())

    def test_opt_without_consent_flag(self):
        self.assertTrue(self.login(False)["token"])
        self.assertEqual(self.saved, [])


if __name__ == "__main__":
    unittest.main()
