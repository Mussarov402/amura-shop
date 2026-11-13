"""Сообщение «заказы не забираются» (владелец 12.11.2026): 502 WB — без HTML и без «проверьте ключ», и только если сбой
длится больше 30 минут; 401/403 — сразу, с подсказкой «Ключи площадок»."""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import marketplaces as mpk  # noqa: E402
import order_hook as oh  # noqa: E402

HTML = "<html>\n<head><title>502 Bad Gateway</title>\n<style>html{color-scheme:light dark;}</style>\n</head>\n<body>\n<center><h1>502 Bad Gateway</h1></center>\n<hr><center>Angie</center>\n</body>\n</html>"


class Ch:
    title, key = "WB — AMURA", "wb"

    def __init__(self):
        self.last = {}


class Alerts(unittest.TestCase):
    def setUp(self):
        self.sent = []
        old = oh.alert
        oh.alert = lambda key, text, every=0: self.sent.append(text)
        self.addCleanup(lambda: setattr(oh, "alert", old))

    def test_gateway_html_is_short(self):
        self.assertEqual(mpk._plain(HTML), "502 Bad Gateway")
        self.assertEqual(mpk._plain("plain error"), "plain error")

    def test_502_waits_30_min_and_no_key_hint(self):
        ch, e = Ch(), mpk.ApiError(502, "HTTP 502: 502 Bad Gateway")
        self.assertIsNone(mpk.orders_alert(ch, e, 1000))
        self.assertIsNone(mpk.orders_alert(ch, e, 1000 + 600))
        t = mpk.orders_alert(ch, e, 1000 + 1900)
        self.assertIn("не отвечает (502)", t)
        self.assertIn("Ключ в порядке", t)
        self.assertNotIn("Render", t)
        self.assertNotIn("<", t)
        self.assertEqual(len(self.sent), 1)

    def test_bad_key_now(self):
        t = mpk.orders_alert(Ch(), mpk.ApiError(401, "HTTP 401: unauthorized"), 5)
        self.assertIn("Ключи площадок", t)
        self.assertEqual(len(self.sent), 1)

    def test_stocks_rate_limit_after_3_cycles(self):
        ch, e = Ch(), mpk.ApiError(429, "You have reached request rate limit per second")
        self.assertIsNone(mpk.stocks_alert(ch, e))
        self.assertIsNone(mpk.stocks_alert(ch, e))
        t = mpk.stocks_alert(ch, e)
        self.assertIn("остатки не обновляются 45 минут", t)
        self.assertIn("ограничивает частоту запросов", t)
        self.assertEqual(len(self.sent), 1)

    def test_ozon_call_retries_rate_limit(self):
        import time
        calls = []

        def post(url, json=None, headers=None, timeout=None):
            calls.append(1)
            code = 429 if len(calls) < 3 else 200
            return type("R", (), {"status_code": code, "text": "You have reached request rate limit per second" if code == 429 else "{}",
                                  "json": lambda s: {"result": []} if code == 200 else {"message": "rate limit"}})()
        old_post, old_sleep = mpk.requests.post, time.sleep
        mpk.requests.post, mpk.time.sleep = post, lambda s: None
        try:
            ch = mpk.Ozon(1)
            self.assertEqual(ch.call("/v2/products/stocks", {}), {"result": []})
        finally:
            mpk.requests.post, mpk.time.sleep = old_post, old_sleep
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
