"""Вход на сайт через WhatsApp (слово владельца 31.10.2026): покупатель сам пишет код на основную линию — бесплатно для магазина.
Номер отправителя — тот же клиент, что по SMS; код не попадает в «Сообщения»; ответ — обычный текст, без шаблона."""
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
from flask import Flask  # noqa: E402

import inbox  # noqa: E402
import order_hook as oh  # noqa: E402
import wa  # noqa: E402

TOO_MANY = oh.too_many          # настоящий лимит: test_checkout / test_retail подменяют его на весь процесс (при импорте ещё настоящий)


def payload(frm, text, mid):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "P1"}, "contacts": [{"wa_id": frm, "profile": {"name": "Аня"}}],
                                              "messages": [{"id": mid, "from": frm, "type": "text", "text": {"body": text}}]}}]}]}


class WaLogin(unittest.TestCase):
    def setUp(self):
        self.sent, self.inbox, self.created = [], [], []
        self.on = True
        self.patch(wa, "configured", lambda: self.on)
        self.patch(wa, "cfg", lambda: {"phone_id": "P1", "token": "T1", "secret": "", "app_id": "", "config_id": "", "config_coex": "", "waba": ""})
        self.patch(wa, "extra_lines", lambda: [])
        self.patch(wa, "check", lambda phone_id="": {"display_phone_number": "+7 701 555 00 11"})
        self.patch(wa, "_call", lambda method, path, ln=None, **kw: self.sent.append((path, kw.get("json"))) or {})
        self.patch(inbox, "on_client_message", lambda *a, **k: self.inbox.append(a))
        self.known = {"77011112233": {"id": "cid-old", "tags": ["сайт"], "name": "Тамирис", "phone": "+77011112233"}}
        self.patch(oh, "cp_by_phone", lambda p: self.known.get(p))
        self.patch(oh, "profile", lambda c: {"city": "Алматы" if c["id"] == "cid-old" else ""})

        def ms(method, path, json=None, **kw):
            if method == "POST" and path == "/entity/counterparty":
                self.created.append(json)
                return {"id": "cid-new", "tags": json["tags"]}
            raise AssertionError((method, path))
        self.patch(oh, "ms", ms)
        oh._cache.pop("wa_login_phone", None)
        oh._wa_login.clear()
        oh._hits.pop("wa:127.0.0.1", None)
        self.patch(oh, "client_ip", lambda: "127.0.0.1")
        self.patch(oh, "too_many", TOO_MANY)
        app = Flask(__name__)
        app.testing = True
        app.register_blueprint(oh.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def start(self):
        r = self.c.post("/auth/wa/start")
        j = r.get_json()
        self.assertTrue(j and j["ok"], (r.status_code, r.data[:300]))
        return j

    def test_start_link_and_poll_known_client(self):
        j = self.start()
        self.assertEqual(j["phone"], "77015550011")
        self.assertEqual(j["text"], f"Код входа AMURA: login_{j['nonce']}")
        self.assertEqual(self.c.get("/auth/wa/poll?nonce=" + j["nonce"]).get_json()["token"], None)
        wa.handle(wa.parse(payload("77011112233", j["text"], "m1"))[0])
        self.assertEqual(self.inbox, [])                                            # код входа — не в «Сообщения»
        self.assertEqual(self.sent[-1][1]["text"]["body"], "Вы вошли на сайт AMURA, вернитесь на страницу.")
        self.assertEqual(self.sent[-1][1]["type"], "text")                          # обычный текст, не шаблон
        p = self.c.get("/auth/wa/poll?nonce=" + j["nonce"]).get_json()
        body = json.loads(oh.base64.urlsafe_b64decode(p["token"].split(".")[0] + "=="))
        self.assertEqual(body["c"], "cid-old")
        self.assertFalse(p["needProfile"])
        self.assertEqual(self.created, [])                                          # тот же клиент, что по SMS
        self.assertEqual(self.c.get("/auth/wa/poll?nonce=" + j["nonce"]).status_code, 404)   # одноразовый

    def test_retail_consent_saved(self):
        saved = []
        self.patch(oh, "save_consent", lambda cid, how: saved.append((cid, how)))
        j = self.start()
        wa.handle(wa.parse(payload("77011112233", j["text"], "m7"))[0])
        self.c.get(f"/auth/wa/poll?nonce={j['nonce']}&consent=1")
        self.assertEqual(saved, [("cid-old", "вход через WhatsApp")])

    def test_new_client_needs_profile(self):
        j = self.start()
        wa.handle(wa.parse(payload("77019998877", j["text"], "m2"))[0])
        p = self.c.get("/auth/wa/poll?nonce=" + j["nonce"]).get_json()
        self.assertTrue(p["needProfile"])
        self.assertEqual((self.created[0]["phone"], self.created[0]["name"]), ("+77019998877", "Аня"))

    def test_expired_and_reused_code(self):
        j = self.start()
        oh._wa_login[j["nonce"]]["t"] = time.time() - 601
        wa.handle(wa.parse(payload("77011112233", j["text"], "m3"))[0])
        self.assertIn("устарел", self.sent[-1][1]["text"]["body"])
        self.assertEqual(self.inbox, [])
        self.assertEqual(self.c.get("/auth/wa/poll?nonce=" + j["nonce"]).status_code, 404)
        j = self.start()
        wa.handle(wa.parse(payload("77011112233", j["text"], "m4"))[0])
        wa.handle(wa.parse(payload("77019998877", j["text"], "m5"))[0])             # чужой номер с тем же кодом — не перехватит вход
        self.assertIn("устарел", self.sent[-1][1]["text"]["body"])
        self.assertEqual(self.created, [])

    def test_ordinary_messages_go_to_inbox(self):
        wa.handle(wa.parse(payload("77011112233", "Здравствуйте, есть крем Anua?", "m6"))[0])
        self.assertEqual(len(self.inbox), 1)
        self.assertEqual(self.sent, [])

    def test_not_configured_no_button(self):
        self.on = False
        self.assertEqual(self.c.post("/auth/wa/start").status_code, 503)

    def test_rate_limit(self):
        codes = [self.c.post("/auth/wa/start").status_code for _ in range(22)]
        self.assertIn(429, codes)


if __name__ == "__main__":
    unittest.main()
