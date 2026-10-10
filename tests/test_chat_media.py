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


class ChatMedia(unittest.TestCase):
    """Видеокружок и геопозиция из панели."""
    def setUp(self):
        self.tg = []

        def tg(method, **data):
            self.tg.append((method, data))
            key = {"sendVideoNote": "video_note", "sendVideo": "video"}.get(method)
            return {"ok": True, "result": {key: {"file_id": "F1"}} if key else {"message_id": 1}}
        self.patch(oh, "tg", tg)
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Т"})
        self.patch(admin, "_convert", lambda data, args, suffix_in=".bin": b"MP4" if args[-1] == ".mp4" else None)
        self.wa = []
        self.patch(admin.wa, "_call", lambda method, path, **kw: self.wa.append(kw.get("json")) or {})
        self.patch(admin.wa, "cfg", lambda: {"phone_id": "P"})
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def conv(self, chat):
        with inbox.db() as d:
            d.run("DELETE FROM conv WHERE chat_id=%s", (chat,))
            return d.run("INSERT INTO conv (chat_id, name, username, status, unread, last_at) VALUES (%s,'Клиент','','ai',0,0)", (chat,), ins=True)

    def last(self, cid):
        return self.c.get(f"/admin/api/inbox/conv/{cid}").get_json()["messages"][-1]

    def test_round_video_telegram(self):
        cid = self.conv("777")
        r = self.c.post(f"/admin/api/inbox/conv/{cid}/send-file", data={"kind": "round", "dur": "4", "file": (io.BytesIO(b"webm"), "round.webm", "video/webm")},
                        content_type="multipart/form-data")
        self.assertTrue(r.get_json()["ok"])
        method, data = self.tg[-1]
        self.assertEqual(method, "sendVideoNote")
        self.assertEqual(data["_files"]["video_note"][1], b"MP4")
        m = self.last(cid)["media"]
        self.assertEqual((m["t"], m["round"]), ("video", 1))

    def test_location(self):
        cid = self.conv("778")
        self.assertTrue(self.c.post(f"/admin/api/inbox/conv/{cid}/send-location", json={"lat": 43.238, "lon": 76.889}).get_json()["ok"])
        self.assertEqual(self.tg[-1][0], "sendLocation")
        self.assertIn("maps.google.com/?q=43.238000,76.889000", self.last(cid)["text"])
        wcid = self.conv("wa:77012345678")
        self.assertTrue(self.c.post(f"/admin/api/inbox/conv/{wcid}/send-location", json={"lat": 43.2, "lon": 76.9}).get_json()["ok"])
        self.assertEqual(self.wa[-1]["type"], "location")
        self.assertEqual(self.c.post(f"/admin/api/inbox/conv/{cid}/send-location", json={"lat": "x"}).status_code, 400)
        self.assertEqual(self.c.post(f"/admin/api/inbox/conv/{cid}/send-location", json={"lat": 100, "lon": 0}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
