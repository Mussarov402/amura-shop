"""Отзывы: только вошедшим, проверка перед публикацией, сводка звёзд, выключатель модуля."""
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
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

from flask import Flask  # noqa: E402
import inbox  # noqa: E402
import order_hook as oh  # noqa: E402
import reviews  # noqa: E402


class Reviews(unittest.TestCase):
    def setUp(self):
        with reviews.db() as d:
            d.run("DELETE FROM review")
            for k in ("reviews_on", "reviews_premod"):
                d.run("DELETE FROM setting WHERE key=%s", (k,))
        oh._cache.pop("rv_sum", None)
        self.me = None
        self.sent = []
        self.patch(oh, "session_cid", lambda: self.me)
        self.patch(oh, "catalog", lambda: {"p1": {"name": "Крем"}})
        self.patch(oh, "notif_on", lambda k: True)
        self.patch(oh, "notify_bg", lambda fn: self.sent.append(fn))
        self.patch(oh, "too_many", lambda *a, **k: False)

        def ms(method, path, **kw):
            if "counterparty" in path:
                return {"name": "Аружан Сериккызы (Алматы)", "actualAddress": "Алматы", "phone": "+77012345678"}
            return {"rows": [{"positions": {"rows": [{"assortment": {"id": "p1"}}]}}]}
        self.patch(oh, "ms", ms)
        app = Flask(__name__)
        app.register_blueprint(reviews.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def post(self, **d):
        return self.c.post("/reviews", data=json.dumps(d), content_type="application/json")

    def test_flow(self):
        self.assertEqual(self.post(id="p1", rating=5, text="Отлично").status_code, 401)      # без входа нельзя
        self.me = "c1"
        self.assertEqual(self.post(id="p1", rating=0).status_code, 400)
        self.assertEqual(self.post(id="p1", rating=2, text="").status_code, 400)            # плохая оценка — нужен текст
        r = json.loads(self.post(id="p1", rating=5, text="Отлично увлажняет").data)
        self.assertEqual(r["status"], "new")                                                  # по умолчанию — на проверку
        self.assertEqual(len(self.sent), 1)                                                   # владельцу сообщение
        j = json.loads(self.c.get("/reviews?id=p1").data)
        self.assertEqual(j["items"], [])
        self.assertEqual(j["mine"]["status"], "new")
        with reviews.db() as d:
            d.run("UPDATE review SET status='ok'")
        oh._cache.pop("rv_sum", None)
        j = json.loads(self.c.get("/reviews?id=p1").data)
        self.assertEqual(j["count"], 1)
        self.assertEqual(j["items"][0]["name"], "Аружан")
        self.assertTrue(j["items"][0]["verified"])
        self.assertEqual(json.loads(self.c.get("/reviews/summary").data)["items"], {"p1": [5.0, 1]})
        self.post(id="p1", rating=4, text="Передумала, хорошо")                               # повторный — правка, не второй отзыв
        with reviews.db() as d:
            self.assertEqual(d.run("SELECT COUNT(*) FROM review", one=True)[0], 1)

    def test_module_off(self):
        with reviews.db() as d:
            inbox.set_setting(d, "reviews_on", "0")
        self.me = "c1"
        self.assertEqual(self.post(id="p1", rating=5, text="ок").status_code, 403)
        self.assertFalse(json.loads(self.c.get("/reviews/summary").data)["on"])

    def test_no_premod(self):
        with reviews.db() as d:
            inbox.set_setting(d, "reviews_premod", "0")
        self.me = "c2"
        self.assertEqual(json.loads(self.post(id="p1", rating=5, text="Супер").data)["status"], "ok")
        self.assertEqual(json.loads(self.c.get("/reviews?id=p1").data)["count"], 1)


if __name__ == "__main__":
    unittest.main()
