"""Согласие с офертой и политикой (розница): факт и время — в комментарии заказа и у клиента при входе по SMS; опт не трогаем."""
import hashlib
import json
import os
import sys
import tempfile
import time
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "fonts"), exist_ok=True)
for _f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
    if not os.path.exists(os.path.join(ROOT, "fonts", _f)):
        import shutil
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

from flask import Flask  # noqa: E402
import order_hook as oh  # noqa: E402

ITEM = {"id": "p1", "name": "Крем", "qty": 50, "opt": 5000, "mid": 4900, "box": 4800, "boxQty": 40, "rtl": 6690}


class Consent(unittest.TestCase):
    def setUp(self):
        self.posted = []
        for name, val in {"catalog": lambda: {"p1": dict(ITEM)}, "too_many": lambda *a, **k: False,
                          "find_or_create_agent": lambda *a: "agent-1", "organization": lambda: "org-1", "store_id": lambda: "store-1",
                          "fee_service_id": lambda: "fee-1", "delivery_service_id": lambda: "dlv-1", "notify_bg": lambda fn: None,
                          "is_wholesale": lambda cid: True, "opt_min": lambda: 0, "mark_retail": lambda *a: None,
                          "_post_order": lambda body: self.posted.append(body) or {"name": str(1500 + len(self.posted)), "id": "o" + str(len(self.posted))}}.items():
            self.patch(oh, name, val)

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def order(self, **extra):
        d = {"name": "Тамирис", "phone": "+77012345678", "city": "Алматы", "shipping": "pickup", "items": [{"id": "p1", "qty": 2}]} | extra
        res, st = oh.order_core(d, "c" + str(len(self.posted)) + json.dumps(extra), "1.1.1.1", "cid-1")
        self.assertEqual(st, 200, res)
        return self.posted[-1]["description"]

    def test_order_description(self):
        self.assertIn(oh.CONSENT_TXT + ": ", self.order(mode="retail", consent=True))
        self.assertIn("(Алматы)", self.posted[-1]["description"])
        self.assertNotIn(oh.CONSENT_TXT, self.order(mode="retail"))
        self.assertNotIn(oh.CONSENT_TXT, self.order(consent=True))              # опт — без строки согласия
        self.assertNotIn(oh.CONSENT_TXT, self.order(mode="retail", consent="1"))  # только настоящая галочка

    def test_pay_card_mark(self):
        self.assertIn(oh.PAY_CARD_TXT, self.order(mode="retail", pay="card"))
        self.assertNotIn(oh.PAY_CARD_TXT, self.order(mode="retail", pay="link"))
        self.assertNotIn(oh.PAY_CARD_TXT, self.order(pay="card"))                 # опт — без метки

    def test_news_flag(self):
        import inbox
        with inbox.db() as d:
            d.run("DELETE FROM setting WHERE key LIKE 'news:%%'")
        self.assertIn(oh.NEWS_TXT + ": нет", self.order(mode="retail", consent=True, news=False))
        self.assertFalse(oh.get_news("+7 701 234 56 78")["on"])
        self.assertIn(oh.NEWS_TXT + ": да", self.order(mode="retail", consent=True, news=True))
        self.assertTrue(oh.get_news("77012345678")["on"])
        self.order(mode="retail", consent=True, news=False)                     # галочку не отметил — прежнее «да» не снимаем
        self.assertTrue(oh.get_news("77012345678")["on"])
        self.assertNotIn(oh.NEWS_TXT, self.order(news=True))                     # опт — без строки
        self.assertIsNone(oh.get_news("77000000000"))

    def test_broadcast_skips_declined(self):
        import admin
        import groups
        import inbox
        with inbox.db() as d:
            d.run("DELETE FROM setting WHERE key LIKE 'news:%%'")
        oh.save_news("77011112233", False, "тест")
        oh.save_news("77019998877", True, "тест")
        self.patch(groups.admin, "need", lambda p: (lambda f: f))
        app = Flask(__name__)
        self.patch(admin, "is_admin", lambda: True)
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Владелец"})
        app.register_blueprint(groups.bp)
        c = app.test_client()
        sent = []
        self.patch(inbox, "send_text", lambda chat, body: sent.append(chat) or "x")
        self.patch(groups.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})())
        self.patch(groups, "PAUSE", 0)
        with groups.db() as d:
            ids = []
            for chat in ("wa:77011112233", "wa:77019998877", "wa:77015550000"):
                d.run("DELETE FROM conv WHERE chat_id=%s", (chat,))
                cid = d.run("INSERT INTO conv (chat_id, name, username, status, unread, last_at) VALUES (%s,%s,'','ai',0,%s)", (chat, "К", time.time()), ins=True)
                d.run("INSERT INTO msg (conv_id, role, text, at) VALUES (%s,'client','привет',%s)", (cid, time.time()))
                ids.append(cid)
        g = json.loads(c.post("/admin/api/inbox/groups", json={"name": "Акция", "members": ids}).data)["id"]
        r = json.loads(c.post(f"/admin/api/inbox/groups/{g}/send", data={"text": "Скидки!"}).data)
        self.assertEqual((r["total"], r["skipped"]), (3, 1))
        self.assertEqual(sorted(sent), ["wa:77015550000", "wa:77019998877"])   # отказавшемуся — не уходит, без отметки и согласным — как раньше

    def test_login_saves_consent(self):
        cp = {"id": "cid-77", "tags": ["сайт"], "name": "Тамирис", "phone": "+77011112233"}
        self.patch(oh, "cp_by_phone", lambda p: dict(cp))
        self.patch(oh, "profile", lambda c: {"city": "Алматы"})
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        c = app.test_client()

        def verify(**kw):
            oh._codes["77011112233"] = {"h": hashlib.sha256(b"123456" + b"77011112233").hexdigest(), "t": time.time(), "tries": 0}
            return c.post("/auth/sms/verify", json={"phone": "77011112233", "code": "123456"} | kw)
        self.assertEqual(verify().status_code, 200)
        self.assertIsNone(oh.get_consent("cid-77"))                            # без галочки (опт) — ничего не пишем
        self.assertTrue(json.loads(verify(consent=True).data)["ok"])
        v = oh.get_consent("cid-77")
        self.assertEqual(v["how"], "вход по SMS")
        self.assertIn("(Алматы)", v["at"])


if __name__ == "__main__":
    unittest.main()
