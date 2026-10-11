"""Город по геопозиции для подсказки «Ваш город — …?» на розничном сайте."""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import order_hook as oh  # noqa: E402
import checkout  # noqa: E402
from flask import Flask  # noqa: E402


class R:
    def __init__(self, j): self.j = j
    def raise_for_status(self): pass
    def json(self): return self.j


class GeoCity(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__); app.register_blueprint(checkout.bp); self.c = app.test_client()
        oh._cache.clear()

    def test_city(self):
        with mock.patch.object(oh.requests, "get", return_value=R({"address": {"city": "Алматы", "country_code": "kz"}})) as g:
            j = self.c.get("/geo/city?lat=43.24&lon=76.91").get_json()
            self.assertEqual(j["city"], "Алматы")
            self.c.get("/geo/city?lat=43.241&lon=76.912")          # тот же квадрат — из кэша
            self.assertEqual(g.call_count, 1)

    def test_village_prefix_and_town(self):
        with mock.patch.object(oh.requests, "get", return_value=R({"address": {"village": "село Талгар", "country_code": "kz"}})):
            self.assertEqual(self.c.get("/geo/city?lat=43.3&lon=77.2").get_json()["city"], "Талгар")

    def test_outside_kz_and_bad(self):
        with mock.patch.object(oh.requests, "get") as g:
            self.assertEqual(self.c.get("/geo/city?lat=55.75&lon=37.6").get_json()["city"], "")
            self.assertEqual(self.c.get("/geo/city?lat=x&lon=1").status_code, 400)
            g.assert_not_called()

    def test_osm_down(self):
        with mock.patch.object(oh.requests, "get", side_effect=Exception("timeout")):
            j = self.c.get("/geo/city?lat=43.2&lon=76.9").get_json()
            self.assertEqual((j["ok"], j["city"]), (False, ""))


if __name__ == "__main__":
    unittest.main()
