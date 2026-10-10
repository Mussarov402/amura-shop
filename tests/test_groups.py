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
import time  # noqa: E402

from flask import Flask  # noqa: E402

import admin  # noqa: E402
import groups  # noqa: E402
import inbox  # noqa: E402
import order_hook as oh  # noqa: E402


class Groups(unittest.TestCase):
    """Группы из любых мессенджеров и рассылка: WhatsApp/Instagram — только в окне 24 ч, {имя} подставляется."""
    def setUp(self):
        self.tg, self.wa = [], []
        self.patch(oh, "tg", lambda method, **data: self.tg.append((method, data)) or {"ok": True, "result": {"message_id": 9, "photo": [{"file_id": "P"}]}})
        self.patch(inbox.wa, "send_text", lambda chat, text: self.wa.append((chat, text)))
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Т"})
        self.patch(groups, "PAUSE", 0)
        self.patch(groups.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})())
        with inbox.db() as d:
            for t in ("msg", "conv"):
                d.run(f"DELETE FROM {t}")
        with groups.db() as d:
            for t in ("grp", "grp_member", "bcast", "bcast_item"):
                d.run(f"DELETE FROM {t}")
        self.tgc = self.conv("1001", "Айгерим Алматы", time.time() - 5 * 86400)
        self.wa_new = self.conv("wa:77011111111", "Жанар", time.time() - 3600)
        self.wa_old = self.conv("wa:77022222222", "Сауле", time.time() - 3 * 86400)
        app = Flask(__name__)
        app.register_blueprint(groups.bp)
        app.register_blueprint(admin.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def conv(self, chat, name, last):
        with inbox.db() as d:
            cid = d.run("INSERT INTO conv (chat_id, name, username, status, unread, last_at) VALUES (%s,%s,'','ai',0,%s)", (chat, name, last), ins=True)
            d.run("INSERT INTO msg (conv_id, role, text, at) VALUES (%s,'client','Привет',%s)", (cid, last))
        return cid

    def test_group_and_broadcast(self):
        r = self.c.post("/admin/api/inbox/groups", json={"name": "Оптовики", "members": [self.tgc, self.wa_new, self.wa_old, 99999]}).get_json()
        gid = r["id"]
        g = self.c.get(f"/admin/api/inbox/groups/{gid}").get_json()
        self.assertEqual(len(g["members"]), 3)                                    # несуществующий диалог не добавился
        self.assertEqual({m["id"]: m["open"] for m in g["members"]}, {self.tgc: True, self.wa_new: True, self.wa_old: False})
        s = self.c.post(f"/admin/api/inbox/groups/{gid}/send", data={"text": "{имя}, новое поступление Anua!"}).get_json()
        self.assertEqual((s["total"], s["skipped"]), (3, 1))
        self.assertEqual(self.tg[-1], ("sendMessage", {"chat_id": "1001", "text": "Айгерим, новое поступление Anua!"}))
        self.assertEqual(self.wa, [("wa:77011111111", "Жанар, новое поступление Anua!")])
        b = self.c.get(f"/admin/api/inbox/groups/{gid}").get_json()["broadcasts"][0]
        self.assertEqual((b["sent"], b["skipped"], b["failed"], b["state"]), (2, 1, 0, "done"))
        rep = self.c.get(f"/admin/api/inbox/broadcasts/{b['id']}").get_json()["items"]
        self.assertIn("24 часов", next(i for i in rep if i["id"] == self.wa_old)["error"])
        with inbox.db() as d:                                                       # сообщение видно в диалоге, ИИ не отключён
            self.assertEqual(d.run("SELECT text FROM msg WHERE conv_id=%s ORDER BY id DESC LIMIT 1", (self.tgc,), one=True)[0], "Айгерим, новое поступление Anua!")
            self.assertEqual(d.run("SELECT status FROM conv WHERE id=%s", (self.tgc,), one=True)[0], "ai")

    def test_edit_members_and_delete(self):
        gid = self.c.post("/admin/api/inbox/groups", json={"name": "Тест", "members": [self.tgc]}).get_json()["id"]
        self.c.put(f"/admin/api/inbox/groups/{gid}", json={"name": "VIP", "add": [self.wa_new], "remove": [self.tgc]})
        g = self.c.get(f"/admin/api/inbox/groups/{gid}").get_json()
        self.assertEqual((g["group"]["name"], [m["id"] for m in g["members"]]), ("VIP", [self.wa_new]))
        lst = self.c.get("/admin/api/inbox/groups").get_json()["groups"]
        self.assertEqual((lst[0]["count"], lst[0]["chans"]), (1, {"wa": 1}))
        self.assertEqual(self.c.post("/admin/api/inbox/groups", json={"name": " "}).status_code, 400)
        self.assertEqual(self.c.post(f"/admin/api/inbox/groups/{gid}/send", data={"text": ""}).status_code, 400)
        self.c.delete(f"/admin/api/inbox/groups/{gid}")
        self.assertEqual(self.c.get(f"/admin/api/inbox/groups/{gid}").status_code, 404)

    def test_contacts_search(self):
        r = self.c.get("/admin/api/inbox/contacts?q=7701").get_json()["contacts"]
        self.assertEqual([c["id"] for c in r], [self.wa_new])
        r = self.c.get("/admin/api/inbox/contacts?ch=tg").get_json()["contacts"]
        self.assertEqual([c["name"] for c in r], ["Айгерим Алматы"])

    def test_photo_broadcast(self):
        gid = self.c.post("/admin/api/inbox/groups", json={"name": "Фото", "members": [self.tgc]}).get_json()["id"]
        r = self.c.post(f"/admin/api/inbox/groups/{gid}/send", data={"text": "Новинка", "file": (io.BytesIO(b"\xff\xd8jpg"), "a.jpg", "image/jpeg")},
                        content_type="multipart/form-data").get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(self.tg[-1][0], "sendPhoto")
        self.assertEqual(self.tg[-1][1]["caption"], "Новинка")

    def test_add_ms_clients_and_template(self):
        tg_attr = {"name": oh.ATTR_TGID, "value": "5005"}
        cps = [{"id": "c1", "name": "Камила Днг", "phone": "+7 700 957 27 43", "attributes": [tg_attr]},
               {"id": "c2", "name": "Лаура Актау", "phone": "8 (700) 666-16-05"},
               {"id": "c3", "name": "Розничный покупатель"}]
        self.patch(oh, "ms", lambda m, path, params=None, **k: {"rows": cps} if path == "/entity/counterparty" else next(c for c in cps if path.endswith(c["id"])))
        sent = []
        self.patch(admin.wa, "send_template", lambda chat, name, lang, params: sent.append((chat, name, lang, params)))
        gid = self.c.post("/admin/api/inbox/groups", json={"name": "Все клиенты"}).get_json()["id"]
        r = self.c.put(f"/admin/api/inbox/groups/{gid}", json={"all_clients": True}).get_json()
        self.assertEqual((r["added"], r["nocontact"]), (2, 1))
        g = self.c.get(f"/admin/api/inbox/groups/{gid}").get_json()["members"]
        self.assertEqual(sorted((m["name"], m["channel"]) for m in g), [("Камила Днг", "tg"), ("Лаура Актау", "wa")])
        self.assertEqual(self.c.put(f"/admin/api/inbox/groups/{gid}", json={"all_clients": True}).get_json()["added"], 0)   # повторно — без дублей
        # пустые диалоги не появляются в списке чатов
        self.assertNotIn("Лаура Актау", [c["name"] for c in self.c.get("/admin/api/inbox/convs").get_json()["convs"]])
        s = self.c.post(f"/admin/api/inbox/groups/{gid}/send", data={"text": "{имя}, скидки!", "wa_tpl": "promo_oct", "wa_name": "1"}).get_json()
        self.assertEqual(s["skipped"], 0)
        self.assertEqual(sent, [("wa:77006661605", "promo_oct", "ru", ["Лаура"])])
        self.assertEqual(self.tg[-1], ("sendMessage", {"chat_id": "5005", "text": "Камила, скидки!"}))
        r = self.c.put(f"/admin/api/inbox/groups/{gid}", json={"clients": ["c3"]}).get_json()
        self.assertEqual((r["added"], r["nocontact"]), (0, 1))


if __name__ == "__main__":
    unittest.main()
