"""Модуль «Пожелания клиентов»: анкета /wish, «В пожелания» из чата, топ брендов с наличием, отметки, Excel; выключен — анкеты нет."""
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
import admin  # noqa: E402
import inbox  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402
import wishes  # noqa: E402

LIVE = {"items": [{"id": "p1", "brand": "Medi-Peel", "qty": 12}, {"id": "p2", "brand": "Medi-Peel", "qty": 3}],
        "hiddenItems": [{"id": "p3", "brand": "Jigott", "qty": 0}]}


class Wishes(unittest.TestCase):
    def setUp(self):
        with wishes.db() as d:
            d.run("DELETE FROM wish")
        modules.update("wishes", on=True)
        self.addCleanup(lambda: modules.update("wishes", on=False))
        old = oh._cache.get("live")
        oh._cache["live"] = (time.time(), LIVE)
        self.addCleanup(lambda: oh._cache.__setitem__("live", old) if old else oh._cache.pop("live", None))
        self.patch(oh, "too_many", lambda *a, **k: False)
        self.me = {"role": "owner", "perms": [], "name": "В"}
        self.patch(admin, "who", lambda: self.me)
        self.patch(admin, "is_admin", lambda: self.me["role"] == "owner")
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        app.register_blueprint(wishes.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def test_survey_and_report(self):
        self.assertTrue(json.loads(self.c.get("/wish").data)["ok"])
        self.assertEqual(self.c.post("/wish", json={}).status_code, 400)                  # пустой ответ не принимаем
        r = self.c.post("/wish", json={"want": "Хочу Anua", "brands": ["Anua", "medi-peel", "Anua"], "improve": "Доставка быстрее",
                                       "rating": 4, "retail": True, "phone": "+7 701 000 00 00", "ref": "aigerim"})
        self.assertEqual(r.status_code, 200)
        self.c.post("/wish", json={"brands": ["anua"], "rating": 9})                       # оценка вне 1–5 не пишется
        o = json.loads(self.c.get("/admin/api/wishes?days=30").data)
        self.assertEqual(o["count"], 2)
        self.assertEqual(o["rating"], 4.0)
        top = {t["brand"].lower(): t for t in o["brands"]}
        self.assertEqual(top["anua"]["count"], 2)
        self.assertFalse(top["anua"]["inCatalog"])
        self.assertEqual((top["medi-peel"]["brand"], top["medi-peel"]["qty"], top["medi-peel"]["products"]), ("Medi-Peel", 15, 2))
        it = next(i for i in o["items"] if i["want"])
        self.assertEqual((it["site"], it["ref"], it["phone"]), ("rtl", "aigerim", "77010000000"))
        self.assertEqual(self.c.post(f"/admin/api/wishes/{it['id']}", json={"status": "done"}).status_code, 200)
        self.assertEqual(self.c.post(f"/admin/api/wishes/{it['id']}", json={"status": "x"}).status_code, 400)
        o = json.loads(self.c.get("/admin/api/wishes").data)
        self.assertEqual(o["open"], 1)
        x = json.loads(self.c.post("/admin/api/wishes/export", json={"days": 90}).data)
        self.assertTrue(self.c.get(x["url"]).data.startswith(b"PK"))

    def test_from_chat(self):
        with inbox.db() as d:
            cid = d.run("INSERT INTO conv (chat_id, name, status, unread, last_at, last_text) VALUES ('ig:123', 'Дана', 'ai', 0, 0, '')", ins=True)
            mid = d.run("INSERT INTO msg (conv_id, role, text, at) VALUES (%s, 'client', 'Привезите Jigott и ещё что-нибудь', 0)", (cid,), ins=True)
            mine = d.run("INSERT INTO msg (conv_id, role, text, at) VALUES (%s, 'manager', 'Здравствуйте', 0)", (cid,), ins=True)
        self.me = {"role": "staff", "perms": ["inbox"], "name": "Менеджер"}                 # переносит сотрудник с «Сообщениями»
        self.assertEqual(self.c.post("/admin/api/wishes/from-msg", json={"msg": mid}).status_code, 200)
        self.assertEqual(self.c.post("/admin/api/wishes/from-msg", json={"msg": mid}).status_code, 400)   # дважды не переносим
        self.assertEqual(self.c.post("/admin/api/wishes/from-msg", json={"msg": mine}).status_code, 400)  # только сообщения клиента
        self.assertEqual(self.c.get("/admin/api/wishes").status_code, 403)                 # раздел — только владелец
        self.me = {"role": "owner", "perms": [], "name": "В"}
        it = json.loads(self.c.get("/admin/api/wishes").data)["items"][0]
        self.assertEqual((it["site"], it["name"], it["brands"]), ("ig", "Дана", ["Jigott"]))

    def test_off(self):
        modules.update("wishes", on=False)
        self.assertFalse(json.loads(self.c.get("/wish").data)["ok"])
        self.assertEqual(self.c.post("/wish", json={"want": "x"}).status_code, 404)
        self.assertEqual(self.c.get("/admin/api/wishes").status_code, 404)

    def test_rate_limit(self):
        self.patch(oh, "too_many", lambda *a, **k: True)
        self.assertEqual(self.c.post("/wish", json={"want": "x"}).status_code, 429)


if __name__ == "__main__":
    unittest.main()
