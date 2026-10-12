"""opt.amura.kz: оптовый сайт отдаётся сервером, файлы — с GitHub Pages."""
import unittest
from unittest import mock

import opt_site
from app import app


class R:
    def __init__(self, code, body=b"", ctype="text/html"):
        self.status_code, self.content, self.headers = code, body, {"Content-Type": ctype}


class OptSite(unittest.TestCase):
    def setUp(self):
        opt_site._cache.clear()
        self.c = app.test_client()
        self.urls = []

    def fake(self, table):
        def get(url, **kw):
            self.urls.append(url)
            return table.get(url, R(404, b"nf"))
        return mock.patch.object(opt_site.requests, "get", side_effect=get)

    def test_index_from_opt_folder(self):
        with self.fake({"https://amura.kz/opt/index.html": R(200, b"OPT")}):
            r = self.c.get("/index.html", headers={"Host": "opt.amura.kz"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data, b"OPT")

    def test_index_falls_back_to_root_before_switch(self):
        with self.fake({"https://amura.kz/": R(200, b"ROOT")}):
            r = self.c.get("/", headers={"Host": "opt.amura.kz"})
        self.assertEqual(r.data, b"ROOT")
        self.assertEqual(self.urls, ["https://amura.kz/opt/", "https://amura.kz/"])

    def test_shared_files_from_root_and_cached(self):
        with self.fake({"https://amura.kz/catalog.json": R(200, b"{}", "application/json")}):
            self.c.get("/catalog.json?t=1", headers={"Host": "opt.amura.kz"})
            r = self.c.get("/catalog.json", headers={"Host": "opt.amura.kz"})
        self.assertEqual(r.data, b"{}")
        self.assertEqual(r.headers["Content-Type"], "application/json")
        self.assertEqual(self.urls, ["https://amura.kz/catalog.json"])

    def test_photos_redirect(self):
        r = self.c.get("/img/a.webp", headers={"Host": "opt.amura.kz"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["Location"], "https://amura.kz/img/a.webp")

    def test_no_api_on_opt_host(self):
        self.assertEqual(self.c.post("/order", headers={"Host": "opt.amura.kz"}).status_code, 405)
        with self.fake({}):
            self.assertEqual(self.c.get("/admin", headers={"Host": "opt.amura.kz"}).status_code, 404)

    def test_github_down_keeps_last_copy(self):
        with self.fake({"https://amura.kz/sw.js": R(200, b"SW", "text/javascript")}):
            self.c.get("/sw.js", headers={"Host": "opt.amura.kz"})
        opt_site._cache["sw.js"] = (0, *opt_site._cache["sw.js"][1:])   # устарело
        with mock.patch.object(opt_site.requests, "get", side_effect=opt_site.requests.RequestException):
            r = self.c.get("/sw.js", headers={"Host": "opt.amura.kz"})
        self.assertEqual(r.data, b"SW")

    def test_other_hosts_untouched(self):
        r = self.c.get("/", headers={"Host": "amura-shop-api.onrender.com"})
        self.assertEqual(r.get_json()["service"], "amura-shop-api")

    def test_cors_allows_opt(self):
        r = self.c.options("/order", headers={"Origin": "https://opt.amura.kz"})
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "https://opt.amura.kz")


if __name__ == "__main__":
    unittest.main()
