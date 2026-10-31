"""Вход через Telegram-бот (главный на обоих сайтах, 31.10.2026) и обязательный номер (слово владельца 01.11.2026):
бот просит «📱 Поделиться номером», принимает только свой контакт; клиент — тот же, что по SMS; без номера заказ не оформить."""
import json
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
ME = {"id": 555, "first_name": "Аня", "username": "anya_k"}


class TgLogin(unittest.TestCase):
    def setUp(self):
        self.saved, self.sent, self.created, self.puts, self.tgid = [], [], [], [], {}
        self.cards = {"cid-sms": {"id": "cid-sms", "phone": "+77011112233", "tags": ["сайт"], "description": "Клиент с сайта", "actualAddress": "Алматы"},
                      "cid-tg": {"id": "cid-tg", "phone": "", "tags": ["сайт"], "description": "Клиент с сайта\nTelegram: @anya_k", "actualAddress": "Астана"}}
        self.by_tg = None

        def ms(method, path, json=None, **kw):
            cid = path.rsplit("/", 1)[-1]
            if method == "GET" and cid in self.cards:
                return dict(self.cards[cid])
            if method == "PUT" and cid in self.cards:
                self.puts.append((cid, json))
                self.cards[cid].update({k: v for k, v in json.items() if k in ("phone", "description")})
                return {}
            if method == "POST" and path == "/entity/counterparty":
                self.created.append(json)
                self.cards["cid-new"] = {"id": "cid-new", **json}
                return dict(self.cards["cid-new"])
            raise AssertionError((method, path))
        for name, val in {"too_many": TOO_MANY, "client_ip": lambda: "127.0.0.1", "ms": ms,
                          "cp_by_phone": lambda p: next((dict(c) for c in self.cards.values() if oh.norm_phone(c.get("phone"))[-10:] == oh.norm_phone(p)[-10:]), None),
                          "cp_by_tg": lambda tg_id, u: self.by_tg, "profile": lambda cp: {"city": cp.get("actualAddress", "")},
                          "set_tg_id": lambda cid, tg_id: self.tgid.__setitem__(cid, tg_id), "live_cid": lambda c: c,
                          "tg": lambda method, **k: self.sent.append(k), "save_consent": lambda cid, how: self.saved.append((cid, how))}.items():
            old = getattr(oh, name)
            setattr(oh, name, val)
            self.addCleanup(setattr, oh, name, old)
        oh._hits.pop("tg:127.0.0.1", None)
        oh._tg_login.clear()
        oh._tg_wait.clear()
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        self.c = app.test_client()

    def bot(self, msg):
        """Сообщение боту — как вебхук Telegram (секрет подставляем)."""
        old = oh.HOOK_SECRET
        oh.HOOK_SECRET = "s3"
        self.addCleanup(setattr, oh, "HOOK_SECRET", old)
        return self.c.post("/tg/s3", json={"message": {"chat": {"id": 555}, "from": ME, **msg}})

    def start(self, token=None):
        h = {"Authorization": "Bearer " + token} if token else {}
        n = self.c.post("/auth/tg/start", headers=h).get_json()["nonce"]
        self.bot({"text": f"/start login_{n}"})
        return n

    def poll(self, n, consent=False):
        return self.c.get(f"/auth/tg/poll?nonce={n}" + ("&consent=1" if consent else "")).get_json()

    def contact(self, phone, user_id=555):
        return self.bot({"contact": {"phone_number": phone, "user_id": user_id, "first_name": "Аня"}})

    def test_asks_phone_no_login_without_it(self):
        n = self.start()
        self.assertTrue(json.loads(self.sent[-1]["reply_markup"])["keyboard"][0][0]["request_contact"])   # кнопка «Поделиться номером»
        self.bot({"text": "привет"})                                                                    # текст — просим кнопку
        self.assertIn("нажмите кнопку", self.sent[-1]["text"])
        self.assertIsNone(self.poll(n)["token"])

    def test_foreign_contact_rejected(self):
        n = self.start()
        self.contact("+7 701 999 88 77", user_id=777)                                                    # чужой контакт
        self.assertIn("не ваш номер", self.sent[-1]["text"])
        self.assertIsNone(self.poll(n)["token"])
        self.assertEqual(self.created, [])

    def test_own_contact_merges_with_sms_client(self):
        n = self.start()
        self.contact("87011112233")                                                                      # номер SMS-клиента
        j = self.poll(n, consent=True)
        self.assertEqual(json.loads(oh.base64.urlsafe_b64decode(j["token"].split(".")[0] + "=="))["c"], "cid-sms")
        self.assertFalse(j["needProfile"])
        self.assertEqual(self.created, [])
        self.assertEqual(self.tgid, {"cid-sms": "555"})
        self.assertIn("@anya_k", self.cards["cid-sms"]["description"])                                   # Telegram — дополнительно
        self.assertEqual(self.saved, [("cid-sms", "вход через Telegram")])
        self.assertIn("remove_keyboard", self.sent[-2]["reply_markup"])

    def test_old_tg_client_without_phone_gets_it(self):
        self.by_tg = dict(self.cards["cid-tg"])                                                          # входил раньше через Telegram без номера
        n = self.start()
        self.contact("+77015550011")
        j = self.poll(n)
        self.assertEqual(json.loads(oh.base64.urlsafe_b64decode(j["token"].split(".")[0] + "=="))["c"], "cid-tg")
        self.assertEqual(self.cards["cid-tg"]["phone"], "+77015550011")
        self.assertEqual(self.created, [])

    def test_new_client_created_with_phone(self):
        n = self.start()
        self.contact("+77019998877")
        self.assertTrue(self.poll(n)["needProfile"])
        self.assertEqual((self.created[0]["phone"], self.created[0]["name"]), ("+77019998877", "Аня"))
        self.assertIn("Telegram: @anya_k", self.created[0]["description"])

    def test_old_client_confirms_by_sms_same_card(self):
        import hashlib, time
        tok = oh.make_token("cid-tg")
        oh._codes["77015550011"] = {"h": hashlib.sha256(b"123456" + b"77015550011").hexdigest(), "t": time.time(), "tries": 0}
        j = self.c.post("/auth/sms/verify", json={"phone": "77015550011", "code": "123456"}, headers={"Authorization": "Bearer " + tok}).get_json()
        self.assertEqual(json.loads(oh.base64.urlsafe_b64decode(j["token"].split(".")[0] + "=="))["c"], "cid-tg")   # тот же клиент
        self.assertEqual(self.cards["cid-tg"]["phone"], "+77015550011")
        self.assertEqual(self.created, [])

    def test_order_needs_confirmed_phone(self):
        tok = oh.make_token("cid-tg")
        r = self.c.post("/order", json={"name": "Аня", "phone": "77010000000", "items": []}, headers={"Authorization": "Bearer " + tok})
        self.assertEqual((r.status_code, r.get_json().get("needPhone")), (403, True))                    # введённый номер не считается
        r = self.c.post("/order", json={"name": "Гость", "telegram": "guest_1", "items": []})
        self.assertEqual(r.status_code, 400)                                                             # гость без номера
        self.assertIn("номер", r.get_json()["error"])

    def test_profile_patch_does_not_set_phone(self):
        tok = oh.make_token("cid-tg")
        self.patch_orders()
        self.c.patch("/me", json={"name": "Аня", "city": "Астана", "phone": "77010000000"}, headers={"Authorization": "Bearer " + tok})
        self.assertEqual(self.cards["cid-tg"]["phone"], "")                                              # номер — только подтверждённый

    def patch_orders(self):
        old = oh.client_orders
        oh.client_orders = lambda cid: []
        self.addCleanup(setattr, oh, "client_orders", old)


if __name__ == "__main__":
    unittest.main()
