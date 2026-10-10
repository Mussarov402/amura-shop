"""Баннеры: у розничного сайта свои (в нашей базе), с высотой и картинкой для телефона; загрузка картинок из панели."""
import io
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "fonts"), exist_ok=True)
for _f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
    if not os.path.exists(os.path.join(ROOT, "fonts", _f)):
        import shutil
from flask import Flask  # noqa: E402

import admin  # noqa: E402
import inbox  # noqa: E402
import order_hook as oh  # noqa: E402
import webchat  # noqa: E402


class ChatEdit(unittest.TestCase):
    """Менеджер исправляет отправленное сообщение: Telegram и сайт — на месте, WhatsApp — «Исправление» отдельным сообщением."""
    def setUp(self):
        self.tg = []

        def tg(method, **data):
            self.tg.append((method, data))
            return {"ok": True, "result": {"message_id": 555}}
        self.patch(oh, "tg", tg)
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Т"})
        self.wa = []
        self.patch(inbox.wa, "send_text", lambda chat, text: self.wa.append(text))
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        app.register_blueprint(webchat.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def conv(self, chat):
        with inbox.db() as d:
            d.run("DELETE FROM conv WHERE chat_id=%s", (chat,))
            return d.run("INSERT INTO conv (chat_id, name, username, status, unread, last_at) VALUES (%s,'Клиент','','manager',0,0)", (chat,), ins=True)

    def send(self, cid, text):
        self.assertTrue(self.c.post(f"/admin/api/inbox/conv/{cid}/send", json={"text": text}).get_json()["ok"])
        return self.c.get(f"/admin/api/inbox/conv/{cid}").get_json()["messages"][-1]

    def edit(self, cid, mid, text):
        return self.c.post(f"/admin/api/inbox/conv/{cid}/msg/{mid}/edit", json={"text": text}).get_json()

    def test_telegram_edits_in_place(self):
        cid = self.conv("12345")
        m = self.send(cid, "Цена 5000 ₸")
        r = self.edit(cid, m["id"], "Цена 4500 ₸")
        self.assertEqual(r, {"ok": True, "inplace": True})
        self.assertEqual(self.tg[-1], ("editMessageText", {"chat_id": "12345", "message_id": "555", "text": "Цена 4500 ₸"}))
        m2 = self.c.get(f"/admin/api/inbox/conv/{cid}").get_json()["messages"][-1]
        self.assertEqual((m2["text"], bool(m2["edited"])), ("Цена 4500 ₸", True))

    def test_whatsapp_sends_correction(self):
        cid = self.conv("wa:77012345678")
        m = self.send(cid, "Доставка завтра")
        r = self.edit(cid, m["id"], "Доставка послезавтра")
        self.assertEqual(r, {"ok": True, "inplace": False})
        self.assertEqual(self.wa[-1], "✏️ Исправление:\nДоставка послезавтра")

    def test_only_own_text(self):
        cid = self.conv("12346")
        with inbox.db() as d:
            inbox.save_msg(d, cid, "client", "Здравствуйте")
            mid = d.run("SELECT max(id) FROM msg WHERE conv_id=%s", (cid,), one=True)[0]
        self.assertEqual(self.c.post(f"/admin/api/inbox/conv/{cid}/msg/{mid}/edit", json={"text": "x"}).status_code, 400)
        self.assertEqual(self.c.post(f"/admin/api/inbox/conv/{cid}/msg/999999/edit", json={"text": "x"}).status_code, 404)

    def test_site_chat_gets_edit(self):
        tok = webchat.new_token()
        cid = self.conv(webchat.chat_of(tok))
        m = self.send(cid, "Будет в пятницу")
        self.assertEqual(self.edit(cid, m["id"], "Будет в субботу"), {"ok": True, "inplace": True})
        j = self.c.get(f"/chat/poll?token={tok}&after={m['id']}").get_json()
        self.assertEqual([(e["id"], e["text"]) for e in j["edits"]], [(m["id"], "Будет в субботу")])
        j2 = self.c.get(f"/chat/poll?token={tok}&after={m['id']}&ed={j['edits'][0]['ed']}").get_json()
        self.assertEqual(j2["edits"], [])                    # уже виденная правка повторно не приходит
        self.assertEqual(self.c.get(f"/chat/poll?token={tok}").get_json()["messages"][-1]["ed"], 1)


if __name__ == "__main__":
    unittest.main()
