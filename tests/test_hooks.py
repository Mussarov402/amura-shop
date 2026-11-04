"""Вебхуки при переезде: Telegram с onrender.com → PUBLIC_URL с тем же путём; в журнале только хосты."""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
import hooks  # noqa: E402
import order_hook as oh  # noqa: E402


class Hooks(unittest.TestCase):
    def run_tg(self, url, public="https://api.amura.kz"):
        calls = []

        def tg(method, **k):
            calls.append((method, k))
            if method == "getWebhookInfo":
                return {"result": {"url": url, "max_connections": 40, "allowed_updates": ["message", "callback_query"]}}
            return {"ok": True}
        with mock.patch.object(oh, "tg", tg), mock.patch.object(oh, "PUBLIC_URL", public):
            return hooks.telegram(), calls

    def test_moves_from_onrender(self):
        text, calls = self.run_tg("https://amura-shop-api.onrender.com/tg/SECRET123")
        self.assertEqual(calls[1][0], "setWebhook")
        self.assertEqual(calls[1][1]["url"], "https://api.amura.kz/tg/SECRET123")
        self.assertIn("callback_query", calls[1][1]["allowed_updates"])
        self.assertNotIn("SECRET123", text)

    def test_no_change_when_ours_or_no_public(self):
        for url, pub in (("https://api.amura.kz/tg/S", "https://api.amura.kz"),
                         ("https://amura-shop-api.onrender.com/tg/S", ""),
                         ("https://amura-shop-api.onrender.com/tg/S", "https://amura-shop-api.onrender.com"), ("", "https://api.amura.kz")):
            text, calls = self.run_tg(url, pub)
            self.assertEqual([c[0] for c in calls], ["getWebhookInfo"], url + pub)
            self.assertNotIn("/tg/S", text)

    def test_errors_hide_token(self):
        def boom(*a, **k):
            raise RuntimeError("bot123:ABC failed")
        out = io.StringIO()
        with mock.patch.object(oh, "tg", boom), mock.patch.object(oh, "BOT", "bot123:ABC"), \
                mock.patch.object(hooks, "meta", lambda: "ok"), redirect_stdout(out):
            hooks.check()
        self.assertNotIn("bot123:ABC", out.getvalue())


if __name__ == "__main__":
    unittest.main()
