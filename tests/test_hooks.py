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

    def test_meta_moves_whatsapp(self):
        import wa
        posts = []

        class R:
            def __init__(self, j, code=200):
                self.j, self.status_code = j, code

            def json(self):
                return self.j
        subs = {"data": [{"object": "whatsapp_business_account", "callback_url": "https://amura-shop-api.onrender.com/wa/SEC",
                          "fields": [{"name": "messages", "version": "v21.0"}, {"name": "smb_message_echoes", "version": "v21.0"}]}]}
        cfg = {"app_id": "app", "secret": "s", "token": "t1", "waba": "w1"}
        with mock.patch.object(wa, "cfg", lambda: cfg), mock.patch.object(wa, "extra_lines", lambda: [{"token": "t2", "waba": "w2"}]), \
                mock.patch.object(oh, "PUBLIC_URL", "https://api.amura.kz"), mock.patch.object(oh, "HOOK_SECRET", "SEC"), \
                mock.patch.object(hooks.requests, "get", lambda *a, **k: R(subs)), \
                mock.patch.object(hooks.requests, "post", lambda url, **k: posts.append((url, k)) or R({"success": True})):
            text = hooks.meta()
        self.assertEqual(text, "whatsapp_business_account: amura-shop-api.onrender.com → api.amura.kz")
        self.assertEqual(posts[0][1]["data"]["callback_url"], "https://api.amura.kz/wa/SEC")
        self.assertEqual(posts[0][1]["data"]["fields"], "messages,smb_message_echoes")
        self.assertEqual([(u.rsplit("/", 2)[-2], k["json"]["override_callback_uri"], k["headers"]["Authorization"]) for u, k in posts[1:]],
                         [("w1", "https://api.amura.kz/wa/SEC", "Bearer t1"), ("w2", "https://api.amura.kz/wa/SEC", "Bearer t2")])


if __name__ == "__main__":
    unittest.main()
