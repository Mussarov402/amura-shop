"""Чат на сайте: токен, отправка в «Сообщения», опрос ответов, доступ к файлам, накладная в чат."""
import io
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
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

from flask import Flask  # noqa: E402

import inbox  # noqa: E402
import webchat  # noqa: E402


class WebchatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = Flask(__name__)
        app.register_blueprint(webchat.bp)
        cls.c = app.test_client()

    def setUp(self):
        self.calls = []
        webchat._hits.clear()

        def on_msg(chat, user, text, photo=None, **k):     # как настоящий: сохраняет сообщение клиента и ответ ИИ
            self.calls.append((chat, user, text, photo))
            with inbox.db() as d:
                r = d.run("SELECT id FROM conv WHERE chat_id=%s", (chat,), one=True)
                cid = r[0] if r else d.run("INSERT INTO conv (chat_id, name, username, status, unread, last_at) VALUES (%s,%s,'','ai',0,0)",
                                           (chat, user["first_name"]), ins=True)
                inbox.save_msg(d, cid, "client", text or "", photo, unread=1)
                inbox.save_msg(d, cid, "ai", "Здравствуйте! Чем помочь?")
        inbox.on_client_message = on_msg
        webchat.threading.Thread = lambda target, args, kwargs, daemon: type("T", (), {"start": lambda s: target(*args, **kwargs)})()

    def test_token(self):
        t = webchat.new_token()
        self.assertTrue(webchat.chat_of(t).startswith("web:"))
        self.assertIsNone(webchat.chat_of(t[:-1] + ("0" if t[-1] != "0" else "1")))
        self.assertIsNone(webchat.chat_of("abc"))

    def test_send_and_poll(self):
        tok = self.c.post("/chat/start").get_json()["token"]
        r = self.c.post("/chat/send", json={"token": tok, "text": "Есть Anua тонер?", "name": "Айгерим", "phone": "8 701 234 56 78"})
        self.assertTrue(r.get_json()["ok"])
        chat, user, text, _ = self.calls[-1]
        self.assertEqual(chat, webchat.chat_of(tok))
        self.assertEqual(user["first_name"], "Айгерим +77012345678")
        p = self.c.get(f"/chat/poll?token={tok}").get_json()["messages"]
        self.assertEqual([(m["me"], m["text"]) for m in p], [(True, "Есть Anua тонер?"), (False, "Здравствуйте! Чем помочь?")])
        p2 = self.c.get(f"/chat/poll?token={tok}&after={p[-1]['id']}").get_json()["messages"]
        self.assertEqual(p2, [])
        self.assertEqual(self.c.get("/chat/poll?token=bad.token").status_code, 403)
        self.assertEqual(self.c.post("/chat/send", json={"token": "x.y", "text": "hi"}).status_code, 403)
        self.assertEqual(self.c.post("/chat/send", json={"token": tok, "text": "  "}).status_code, 400)

    def test_photo_and_access(self):
        tok, other = self.c.post("/chat/start").get_json()["token"], self.c.post("/chat/start").get_json()["token"]
        r = self.c.post("/chat/send", data={"token": tok, "text": "чек", "file": (io.BytesIO(b"\xff\xd8jpeg"), "check.jpg", "image/jpeg")},
                        content_type="multipart/form-data")
        self.assertTrue(r.get_json()["ok"])
        fid = self.calls[-1][3]
        self.assertTrue(fid.startswith("web:"))
        self.assertEqual(webchat.download(fid), (b"\xff\xd8jpeg", "image/jpeg"))
        url = [m for m in self.c.get(f"/chat/poll?token={tok}").get_json()["messages"] if m.get("photo")][0]["photo"]
        path = url[url.index("/chat/f/"):]
        self.assertEqual(self.c.get(path).data, b"\xff\xd8jpeg")
        self.assertEqual(self.c.get(path.split("?")[0] + "?token=" + other).status_code, 404)   # чужой токен — нет доступа
        bad = self.c.post("/chat/send", data={"token": tok, "file": (io.BytesIO(b"MZ"), "x.exe", "application/x-msdownload")},
                          content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)

    def test_inbox_channel_and_pdf(self):
        tok = self.c.post("/chat/start").get_json()["token"]
        chat = webchat.chat_of(tok)
        self.c.post("/chat/send", json={"token": tok, "text": "оформите заказ"})
        self.assertEqual(inbox.channel(chat), "web")
        self.assertEqual(inbox.channel_name(chat), "Сайт")
        inbox.send_text(chat, "ничего не отправляется наружу")             # не падает и никуда не ходит
        inbox.send_pdf(chat, "AMURA-1600.pdf", b"%PDF-1.4", "Ваш заказ AMURA № 1600")
        p = self.c.get(f"/chat/poll?token={tok}").get_json()["messages"]
        self.assertEqual(p[-1]["file"]["name"], "AMURA-1600.pdf")
        self.assertIn("Ваш заказ", p[-1]["text"])
        self.assertEqual(inbox.fetch_file(p[-1]["file"]["url"].split("/chat/f/")[1].split("?")[0])[0], b"%PDF-1.4")

    def test_rate_limit(self):
        tok = self.c.post("/chat/start").get_json()["token"]
        codes = [self.c.post("/chat/send", json={"token": tok, "text": f"m{i}"}).status_code for i in range(32)]
        self.assertIn(429, codes)


if __name__ == "__main__":
    unittest.main()
