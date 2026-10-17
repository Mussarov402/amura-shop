"""ИИ поддержки amura.kz: свои правила, только розничные цены, без оформления заказов; статус — только заказы вошедшего покупателя."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402
import inbox  # noqa: E402
import order_hook as oh  # noqa: E402
import webchat  # noqa: E402

ITEMS = [{"id": "p1", "name": "Anua Heartleaf 77% Soothing Toner", "brand": "Anua", "opt": 5200, "mid": 4900, "box": 4600, "boxQty": 40, "rtl": 7900, "qty": 5},
         {"id": "p2", "name": "Anua Only Wholesale", "brand": "Anua", "opt": 3000, "rtl": 0, "qty": 3}]
ORDERS = [{"number": "01234", "date": "17.10.2026", "status": "Отгружен", "sum": 10890, "paid": True, "ship": "курьер по Алматы — Туркебаева 63",
           "items": [{"name": "Крем", "qty": 2, "price": 4195}], "track": {"label": "Курьер в пути", "link": "https://ya.cc/t/abc"}},
          {"number": "01200", "date": "10.10.2026", "status": "Доставлен", "sum": 5000, "paid": True, "ship": "самовывоз", "items": []}]


class Resp:
    def __init__(self, j):
        self.status_code, self._j = 200, j

    def json(self):
        return self._j


class SupportAI(unittest.TestCase):
    def setUp(self):
        self.sent = []
        saved = [(inbox, "catalog_items"), (inbox.requests, "post"), (oh, "client_orders"), (oh, "token_cid")]
        old = [(o, n, getattr(o, n)) for o, n in saved]
        self.addCleanup(lambda: [setattr(o, n, v) for o, n, v in old])
        inbox.catalog_items = lambda: [dict(i) for i in ITEMS]
        oh.client_orders = lambda cid: [dict(o) for o in ORDERS] if cid == "cp-1" else []
        oh.token_cid = lambda t: "cp-1" if t == "good" else None
        self.replies = []

        def post(url, **kw):
            self.sent.append(kw["json"])
            return Resp({"choices": [{"message": self.replies.pop(0)}]})
        inbox.requests.post = post

    def test_retail_prompt_tools_and_prices(self):
        self.replies = [{"content": "Здравствуйте! Тонер есть, 7 900 ₸."}]
        with inbox.db() as d:
            out, hand = inbox.ai_reply(d, [], "сколько стоит Anua toner", ctx={"chat": "web:r-abcdefgh1", "retail": True})
        self.assertEqual((out, hand), ("Здравствуйте! Тонер есть, 7 900 ₸.", False))
        body = self.sent[0]
        system = body["messages"][0]["content"]
        self.assertIn("служба поддержки розничного", system)
        self.assertIn("Anua Heartleaf 77% Soothing Toner", system)
        self.assertIn("7 900 ₸", system)
        for w in ("5 200", "4 900", "короб", "опт 5", "Only Wholesale", "create_order"):
            self.assertNotIn(w, system.split("# Товары из каталога")[1])
        self.assertEqual({t["function"]["name"] for t in body["tools"]}, {"search_catalog", "my_orders"})    # заказ в чате не оформляет

    def test_wholesale_unchanged(self):
        self.replies = [{"content": "ok"}]
        with inbox.db() as d:
            inbox.ai_reply(d, [], "Anua toner", ctx={"chat": "web:abcdefgh1"})
        self.assertIn("create_order", {t["function"]["name"] for t in self.sent[0]["tools"]})
        self.assertIn("опт 5 200 ₸", self.sent[0]["messages"][0]["content"])

    def test_my_orders_only_logged_in_client(self):
        chat = webchat.chat_of(webchat.new_token(retail=True))
        self.assertIn("не вошёл", inbox.my_orders_text({"chat": chat}))
        webchat.bind_client(chat, "cp-1")
        txt = inbox.my_orders_text({"chat": chat}, "№ 01234")
        self.assertIn("№ 01234 от 17.10.2026: Передан в доставку (Курьер в пути), где курьер: https://ya.cc/t/abc", txt)
        self.assertNotIn("01200", txt)
        self.assertIn("№ 01200 от 10.10.2026: Получен", inbox.my_orders_text({"chat": chat}))
        self.assertIn("нет", inbox.my_orders_text({"chat": chat}, "99999"))

    def test_tool_call_round(self):
        chat = webchat.chat_of(webchat.new_token(retail=True))
        webchat.bind_client(chat, "cp-1")
        self.replies = [{"content": None, "tool_calls": [{"id": "c1", "function": {"name": "my_orders", "arguments": json.dumps({"number": "01234"})}}]},
                        {"content": "Заказ № 01234 передан курьеру."}]
        with inbox.db() as d:
            out, hand = inbox.ai_reply(d, [], "где мой заказ 01234", ctx={"chat": chat, "retail": True})
        self.assertEqual(out, "Заказ № 01234 передан курьеру.")
        self.assertIn("Передан в доставку", self.sent[1]["messages"][-1]["content"])
        self.replies = [{"content": None, "tool_calls": [{"id": "c2", "function": {"name": "create_order", "arguments": "{}"}}]}, {"content": "x"}]
        with inbox.db() as d:
            inbox.ai_reply(d, [], "оформи", ctx={"chat": chat, "retail": True})
        self.assertEqual(self.sent[-1]["messages"][-1]["content"], "Неизвестный инструмент")     # розница не может создать заказ

    def test_chat_send_binds_login(self):
        app = Flask(__name__)
        app.register_blueprint(webchat.bp)
        c = app.test_client()
        old = inbox.on_client_message
        inbox.on_client_message = lambda *a, **k: None
        self.addCleanup(lambda: setattr(inbox, "on_client_message", old))
        tok = c.post("/chat/start?site=retail").get_json()["token"]
        chat = webchat.chat_of(tok)
        c.post("/chat/send", json={"token": tok, "text": "где заказ", "auth": "bad"})
        self.assertIsNone(webchat.client_of(chat))                        # чужой или неверный токен входа — не привязываем
        c.post("/chat/send", json={"token": tok, "text": "где заказ", "auth": "good"})
        self.assertEqual(webchat.client_of(chat), "cp-1")
        tok2 = c.post("/chat/start").get_json()["token"]                  # opt.amura.kz — не привязываем
        c.post("/chat/send", json={"token": tok2, "text": "x", "auth": "good"})
        self.assertIsNone(webchat.client_of(webchat.chat_of(tok2)))


if __name__ == "__main__":
    unittest.main()
