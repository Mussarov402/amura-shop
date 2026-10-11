"""WhatsApp: второй номер — ответ уходит с того номера, на который написал клиент; подписи номеров в чатах."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask  # noqa: E402

import admin  # noqa: E402
import inbox  # noqa: E402
import wa  # noqa: E402


def payload(pid, frm, text, mid):
    return {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": pid}, "contacts": [{"wa_id": frm, "profile": {"name": "Аня"}}],
                                              "messages": [{"id": mid, "from": frm, "type": "text", "text": {"body": text}}]}}]}]}


class WaLines(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.patch(wa, "cfg", lambda: {"phone_id": "P1", "token": "T1", "secret": "", "app_id": "", "config_id": "", "config_coex": "", "waba": ""})
        self.patch(wa, "_call", lambda method, path, ln=None, **kw: self.sent.append((path, (ln or {}).get("token"))) or {})
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Т"})
        with inbox.db() as d:
            wa.save_lines(d, [{"phone_id": "P2", "token": "T2", "waba": "W2", "name": "Розница", "phone": "+7 701 000 00 02"}])
            inbox.set_setting(d, "wa_name", "Опт")
            for k in ("wa_cl:wa:77010000001", "wa_cl:wa:77010000002"):
                inbox.set_setting(d, k, "")
        wa.reset_cache()
        wa._chatline.clear()
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        self.c = app.test_client()

    def tearDown(self):
        with inbox.db() as d:
            wa.save_lines(d, [])
        wa.reset_cache()
        wa._chatline.clear()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def test_parse_keeps_line(self):
        m = wa.parse(payload("P2", "77010000001", "Привет", "m1"))[0]
        self.assertEqual(m["line"], "P2")

    def test_reply_goes_from_second_number(self):
        wa.remember_line("wa:77010000001", "P2")
        wa.send_text("wa:77010000001", "Здравствуйте")
        self.assertEqual(self.sent[-1], ("P2/messages", "T2"))
        wa._chatline.clear()                                  # после перезапуска номер диалога берётся из базы
        wa.send_text("wa:77010000001", "Ещё")
        self.assertEqual(self.sent[-1], ("P2/messages", "T2"))

    def test_primary_by_default_and_switch_back(self):
        wa.send_text("wa:77010000002", "Привет")
        self.assertEqual(self.sent[-1], ("P1/messages", "T1"))
        wa.remember_line("wa:77010000002", "P2")
        wa.remember_line("wa:77010000002", "P1")
        wa.send_text("wa:77010000002", "Привет")
        self.assertEqual(self.sent[-1], ("P1/messages", "T1"))

    def test_unknown_line_is_primary(self):
        wa.remember_line("wa:77010000002", "PX")
        wa.send_text("wa:77010000002", "Привет")
        self.assertEqual(self.sent[-1], ("P1/messages", "T1"))

    def test_line_name_in_chat(self):
        wa.remember_line("wa:77010000001", "P2")
        self.assertEqual(wa.line_name("wa:77010000001"), "Розница")
        self.assertEqual(wa.line_name("wa:77010000002"), "Опт")

    def test_no_label_with_one_number(self):
        with inbox.db() as d:
            wa.save_lines(d, [])
        wa.reset_cache()
        self.assertEqual(wa.line_name("wa:77010000001"), "")
        wa.send_text("wa:77010000001", "Привет")
        self.assertEqual(self.sent[-1], ("P1/messages", "T1"))

    def test_channels_rename_and_remove(self):
        j = self.c.get("/admin/api/channels").get_json()
        self.assertEqual(j["wa"]["lines"], [{"phone_id": "P2", "name": "Розница", "phone": "+7 701 000 00 02"}])
        self.assertNotIn("token", json.dumps(j["wa"]["lines"]))
        self.c.put("/admin/api/channels", json={"name": "Опт", "lines": [{"phone_id": "P2", "name": "Магазин"}]})
        self.assertEqual(wa.extra_lines()[0]["name"], "Магазин")
        self.assertEqual(wa.extra_lines()[0]["token"], "T2")
        self.c.put("/admin/api/channels", json={"lines": []})
        self.assertEqual(wa.extra_lines(), [])


if __name__ == "__main__":
    unittest.main()
