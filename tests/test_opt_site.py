"""opt.amura.kz и amura.kz: сайты отдаёт сервер, файлы — с GitHub Pages (pages.amura.kz, затем GitHub напрямую под amura.kz)."""
import unittest
from unittest import mock

import opt_site
from app import app

H = {"text/html": {"Content-Type": "text/html"}}


class OptSite(unittest.TestCase):
    def setUp(self):
        opt_site._cache.clear()
        opt_site._img.clear()
        opt_site._down.clear()
        self.c = app.test_client()
        self.urls = []

    def fake(self, pages=None, github=None, http=None, old=None):
        """Источники: словари путь -> (код, заголовки, тело); None — источник не отвечает (нет DNS, чужой сертификат)."""
        def src(name, table):
            def f(path):
                self.urls.append(f"{name}:{path}")
                if table is None:
                    raise OSError("нет связи")
                return table.get(path, (404, {"Content-Type": "text/html"}, b"nf"))
            return f
        pg, gh, hp, old = src("pages", pages), src("github", github), src("http", http), src("old", old)
        return mock.patch.multiple(opt_site, _github=gh,
                                   _pages=lambda p, scheme="https", host=None: (old if host else pg if scheme == "https" else hp)(p))

    def ok(self, body, ct="text/html"):
        return 200, {"Content-Type": ct}, body

    def test_before_switch_github_by_old_name(self):
        """Сейчас: pages.amura.kz ещё нет — берём с GitHub под amura.kz; опт — из /opt/, иначе из корня."""
        with self.fake(github={"opt/index.html": self.ok(b"OPT")}):
            r = self.c.get("/index.html", headers={"Host": "opt.amura.kz"})
        self.assertEqual((r.status_code, r.data), (200, b"OPT"))
        self.assertEqual(self.urls, ["pages:opt/index.html", "github:opt/index.html"])
        self.urls.clear()
        opt_site._cache.clear()
        with self.fake(github={"": self.ok(b"ROOT")}):
            r = self.c.get("/", headers={"Host": "opt.amura.kz"})
        self.assertEqual(r.data, b"ROOT")
        self.assertEqual(self.urls, ["github:opt/", "http:opt/", "old:opt/", "github:"])    # молчащий источник пропускаем 5 минут

    def test_after_switch_pages_first(self):
        with self.fake(pages={"": self.ok(b"RTL"), "catalog.json": self.ok(b"{}", "application/json")}, github={"": self.ok(b"OLD")}):
            r = self.c.get("/", headers={"Host": "amura.kz"})
            self.assertEqual(r.data, b"RTL")
            self.c.get("/catalog.json?t=1", headers={"Host": "amura.kz"})
            r = self.c.get("/catalog.json", headers={"Host": "amura.kz"})
        self.assertEqual((r.data, r.headers["Content-Type"]), (b"{}", "application/json"))
        self.assertEqual(self.urls, ["pages:", "pages:catalog.json"])            # кэш
        self.assertEqual(r.headers["Strict-Transport-Security"], "max-age=31536000")

    def test_pages_cert_not_ready_http(self):
        with self.fake(pages=None, github={}, http={"shop/legal/privacy.html": self.ok(b"PRIV")}):
            r = self.c.get("/shop/legal/privacy.html", headers={"Host": "amura.kz"})
        self.assertEqual(r.data, b"PRIV")

    def test_old_dns_last_resort(self):
        """Сразу после деплоя (DNS ещё на GitHub): если прямой путь к GitHub не сработал — как раньше, amura.kz по DNS."""
        with self.fake(old={"opt/index.html": self.ok(b"OPT")}):
            r = self.c.get("/index.html", headers={"Host": "opt.amura.kz"})
        self.assertEqual(r.data, b"OPT")

    def test_redirect_to_own_host(self):
        with self.fake(pages={"shop": (301, {"Location": "https://pages.amura.kz/shop/"}, b"")}):
            r = self.c.get("/shop", headers={"Host": "amura.kz"})
        self.assertEqual((r.status_code, r.headers["Location"]), (301, "/shop/"))

    def test_www_to_apex(self):
        r = self.c.get("/shop/?a=1", headers={"Host": "www.amura.kz"})
        self.assertEqual((r.status_code, r.headers["Location"]), (301, "https://amura.kz/shop/?a=1"))

    def test_photos(self):
        r = self.c.get("/img/a.webp", headers={"Host": "opt.amura.kz"})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "https://amura.kz/img/a.webp"))
        with self.fake(pages={"img/a.webp": self.ok(b"IMG", "image/webp")}):
            self.c.get("/img/a.webp", headers={"Host": "amura.kz"})
            r = self.c.get("/img/a.webp", headers={"Host": "amura.kz"})
        self.assertEqual(r.data, b"IMG")
        self.assertEqual(r.headers["Cache-Control"], "public, max-age=86400")
        self.assertEqual(self.urls, ["pages:img/a.webp"])                        # второй раз — из кэша сервера

    def test_loop_guard(self):
        r = self.c.get("/", headers={"Host": "amura.kz", opt_site.LOOP: "1"})
        self.assertEqual(r.status_code, 508)

    def test_no_api_on_site_hosts(self):
        for h in ("opt.amura.kz", "amura.kz"):
            self.assertEqual(self.c.post("/order", headers={"Host": h}).status_code, 405)
            with self.fake(pages={}, github={}):
                self.assertEqual(self.c.get("/admin", headers={"Host": h}).status_code, 404)

    def test_github_down_keeps_last_copy(self):
        with self.fake(pages={"sw.js": self.ok(b"SW", "text/javascript")}):
            self.c.get("/sw.js", headers={"Host": "opt.amura.kz"})
        k = ("opt", "sw.js")
        opt_site._cache[k] = (0, *opt_site._cache[k][1:])                       # устарело
        with self.fake():
            r = self.c.get("/sw.js", headers={"Host": "opt.amura.kz"})
        self.assertEqual(r.data, b"SW")

    def test_other_hosts_untouched(self):
        r = self.c.get("/", headers={"Host": "amura-shop-api.onrender.com"})
        self.assertEqual(r.get_json()["service"], "amura-shop-api")
        self.assertNotIn("Strict-Transport-Security", r.headers)

    def test_cors_allows_opt(self):
        r = self.c.options("/order", headers={"Origin": "https://opt.amura.kz"})
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "https://opt.amura.kz")


if __name__ == "__main__":
    unittest.main()
