"""Согласие (оба сайта): факт, время и версия — в журнале согласий по заказу и у клиента; первое сообщение ИИ и «оператор»."""
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
with open(os.path.join(ROOT, "retail", "legal", "requisites.json"), encoding="utf-8") as _f:
    PRIV = json.load(_f)["privacy_date"]          # дата редакции политики — из опубликованных реквизитов
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
        with oh._consent_db() as d:
            d.run("DELETE FROM consent_log")
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
        if d.get("mode") == "retail" and d.get("shipping") == "pickup":     # на розничном сайте самовывоза нет — пункт СДЭК
            d = {**d, "shipping": "cdek", "pvz": {"code": "A1", "address": "Абая 1"}}
        res, st = oh.order_core(d, "c" + str(len(self.posted)) + json.dumps(extra), "1.1.1.1", "cid-1")
        self.assertEqual(st, 200, res)
        return self.posted[-1]["description"]

    def test_order_description(self):
        """Согласие — не в комментарии заказа, а в журнале согласий по номеру заказа (владелец 06.11.2026)."""
        desc = self.order(mode="retail", consent=True)
        self.assertNotIn(oh.CONSENT_TXT, desc)
        self.assertNotIn("политика от", desc)
        c = oh.order_consent(self.num())
        self.assertIn("(Алматы)", c["at"])
        self.assertEqual(c["site"], "rtl")
        self.assertIn("политика от " + PRIV, c["ver"])                          # версия — дата редакции политики
        self.assertNotIn(oh.CONSENT_TXT, self.order(mode="retail"))
        self.assertIsNone(oh.order_consent(self.num()))
        self.assertNotIn(oh.CONSENT_TXT, self.order(consent=True))              # опт — тоже в журнал
        self.assertEqual(oh.order_consent(self.num())["site"], "opt")
        self.order(mode="retail", consent="1")                                   # только настоящая галочка
        self.assertIsNone(oh.order_consent(self.num()))
        with oh._consent_db() as d:
            row = d.run("SELECT ip, text, cid FROM consent_log WHERE number=%s", (str(1500 + 3),), one=True)
        self.assertEqual(row, ("1.1.1.1", oh.CONSENT_TEXT, "cid-1"))              # доказательство: IP, текст, клиент

    def num(self):
        return str(1500 + len(self.posted))

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
        self.patch(groups, "wholesale_phones", lambda: set())
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
        self.assertEqual((r["total"], r["skipped"]), (3, 2))
        self.assertEqual(sent, ["wa:77019998877"])          # по согласию (Юрист, 05.11.2026): только с галочкой «да»; без отметки и отказавшимся — нет
        self.patch(groups, "wholesale_phones", lambda: {"77015550000"})          # оптовик без отметки — пока как раньше (до слова владельца)
        sent.clear()
        r = json.loads(c.post(f"/admin/api/inbox/groups/{g}/send", data={"text": "Прайс"}).data)
        self.assertEqual(sorted(sent), ["wa:77015550000", "wa:77019998877"])
        with inbox.db() as d:
            inbox.set_setting(d, groups.OPT_FLAG, "1")                           # владелец включил правило и для опта
        sent.clear()
        try:
            r = json.loads(c.post(f"/admin/api/inbox/groups/{g}/send", data={"text": "Прайс"}).data)
            self.assertEqual(sent, ["wa:77019998877"])
        finally:
            with inbox.db() as d:
                d.run("DELETE FROM setting WHERE key=%s", (groups.OPT_FLAG,))

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
        self.assertEqual(v["ver"], "текст от 03.11.2026, политика от " + PRIV)
        self.assertIn("в том числе их передачу за рубеж", v["text"])

    def test_ai_hello_operator_mask(self):
        import inbox
        sent, calls = [], []
        self.patch(inbox, "send_text", lambda chat, text, human=True: sent.append(text) or "x")
        self.patch(inbox, "ai_on", lambda d, chat: True)
        self.patch(inbox, "ai_reply", lambda d, hist, text, *a, **k: calls.append(text) or ("Ответ ИИ", False))
        self.patch(oh, "notif_on", lambda k: False)
        chat = "tg:hello-" + str(time.time())
        inbox.on_client_message(chat, {"first_name": "Аня"}, "Здравствуйте")
        self.assertEqual(sent, [inbox.AI_HELLO, "Ответ ИИ"])                    # новый диалог — сначала предупреждение
        inbox.on_client_message(chat, {"first_name": "Аня"}, "Цена?")
        self.assertEqual(sent[2:], ["Ответ ИИ"])                                # дальше — без повтора
        inbox.on_client_message(chat, {"first_name": "Аня"}, "Позовите Оператора")
        self.assertEqual(sent[3:], [inbox.OPERATOR_ACK])                        # «оператор» — сразу сотруднику, без ИИ
        self.assertEqual(len(calls), 2)
        with inbox.db() as d:
            self.assertEqual(d.run("SELECT status FROM conv WHERE chat_id=%s", (chat,), one=True)[0], "manager")
        self.assertEqual(inbox.mask_pd("ИИН 030202 500244, тел 77011112233"), "ИИН [ИИН скрыт], тел 77011112233")


if __name__ == "__main__":
    unittest.main()
