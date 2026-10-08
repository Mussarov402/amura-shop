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
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

from flask import Flask  # noqa: E402
import admin  # noqa: E402
import inbox  # noqa: E402
import order_hook as oh  # noqa: E402

OPT = {"autoplaySec": 6, "slides": [{"title": "Опт", "text": "", "bg": "#14503C"}]}


class Banners(unittest.TestCase):
    def setUp(self):
        self.puts = []

        def ms(method, path, **kw):
            if method == "PUT":
                self.puts.append(kw["json"])
                return {}
            if path.startswith("/entity/organization/"):
                return {"attributes": [{"name": admin.ATTR_BANNERS, "value": json.dumps(OPT)}]}
            raise AssertionError(path)
        self.patch(oh, "ms", ms)
        self.patch(oh, "organization", lambda: "org")
        self.patch(admin, "_attr", lambda *a: {"meta": {}})
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Т"})
        with inbox.db() as d:
            d.run("DELETE FROM setting WHERE key='banners_retail'")
        for k in ("banners", "banners_retail"):
            oh._cache.pop(k, None)
        app = Flask(__name__)
        app.register_blueprint(admin.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def test_retail_separate_from_opt(self):
        self.assertEqual(self.c.get("/banners?site=retail").status_code, 404)        # пока не сохранили — сайт берёт свои по умолчанию
        body = {"autoplaySec": 5, "size": "l", "slides": [
            {"title": "", "img": "https://x/a.jpg", "imgM": "https://x/m.jpg"},
            {"title": "Скрыт", "off": True}, {"title": "Т", "imgM": "https://x/m2.jpg"}]}
        r = json.loads(self.c.put("/admin/api/banners?site=retail", data=json.dumps(body), content_type="application/json").data)
        self.assertTrue(r["ok"])
        self.assertEqual(self.puts, [])                                                  # в МойСклад не пишем
        pub = json.loads(self.c.get("/banners?site=retail").data)
        self.assertEqual(pub["size"], "l")
        self.assertEqual(pub["slides"][0]["imgM"], "https://x/m.jpg")
        self.assertEqual([s["title"] for s in pub["slides"]], ["", "Т"])
        self.assertNotIn("imgM", pub["slides"][1])                                       # телефонная — только вместе с основной картинкой
        opt = json.loads(self.c.get("/banners").data)
        self.assertEqual(opt["slides"][0]["title"], "Опт")
        self.assertNotIn("size", opt)

    def test_bad_size_falls_back(self):
        self.c.put("/admin/api/banners?site=retail", data=json.dumps({"size": "xxl", "slides": [{"title": "А"}]}), content_type="application/json")
        self.assertEqual(json.loads(self.c.get("/admin/api/banners?site=retail").data)["size"], "m")

    def test_retail_defaults_in_panel(self):
        r = json.loads(self.c.get("/admin/api/banners?site=retail").data)
        self.assertFalse(r["saved"])
        self.assertTrue(r["slides"])

    def test_opt_still_in_moysklad(self):
        self.c.put("/admin/api/banners", data=json.dumps({"slides": [{"title": "Новый"}]}), content_type="application/json")
        self.assertEqual(len(self.puts), 1)

    def test_upload_and_serve(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
        r = self.c.post("/admin/api/banners/upload", data={"file": (io.BytesIO(png), "b.png", "image/png")}, content_type="multipart/form-data")
        url = json.loads(r.data)["url"]
        self.assertTrue(url.startswith("https://"))
        g = self.c.get(url.split("localhost", 1)[1])
        self.assertEqual(g.data, png)
        self.assertEqual(g.mimetype, "image/png")
        bad = self.c.post("/admin/api/banners/upload", data={"file": (io.BytesIO(b"x"), "a.pdf", "application/pdf")}, content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)

    def test_upload_owner_only(self):
        self.patch(admin, "who", lambda: {"role": "staff", "perms": ["products"], "name": "С"})
        r = self.c.post("/admin/api/banners/upload", data={"file": (io.BytesIO(b"x"), "b.png", "image/png")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
