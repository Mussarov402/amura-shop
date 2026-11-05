"""Товары МойСклад для новых карточек WB/Ozon (05.11.2026): чтение агента по кодам и по группе — с себестоимостью по правилу
калькулятора; /pub/msimg — только фото товара по коду, JPEG, кэш на диске, без токена."""
import io
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask  # noqa: E402
from PIL import Image  # noqa: E402

import agent  # noqa: E402
import marketplaces as mpk  # noqa: E402
import modules  # noqa: E402
import mp_calc  # noqa: E402
import ms_goods  # noqa: E402
import order_hook as oh  # noqa: E402

TOKEN = "secret-token-123"
PID = "11111111-2222-3333-4444-555555555555"
ROW = {"id": PID, "code": "01300", "article": "A-1", "name": "Крем AMURA", "description": "Увлажняющий", "pathName": "Бренды/AMURA",
       "barcodes": [{"ean13": "4600000000017"}], "buyPrice": {"value": 1250, "currency": {"isoCode": "KZT"}},
       "weight": 0.2, "volume": 0.25, "country": {"name": "Корея"}, "archived": False,
       "attributes": [{"name": "Ширина, см", "value": 5}, {"name": "Высота, см", "value": 12}, {"name": "Глубина, см", "value": 5}]}


def png():
    b = io.BytesIO()
    Image.new("RGBA", (40, 30), (255, 0, 0, 0)).save(b, "PNG")
    return b.getvalue()


class Goods(unittest.TestCase):
    def setUp(self):
        os.environ["MP_AGENT_TOKEN"] = TOKEN
        self.addCleanup(lambda: os.environ.pop("MP_AGENT_TOKEN", None))
        modules.update("agent", on=True)
        self.addCleanup(lambda: modules.update("agent", on=False))
        agent._hits.clear()
        self.ms_calls, self.dl = [], []

        def ms(method, path, params=None, **k):
            self.ms_calls.append((path, params))
            if path == "/entity/product":
                f = (params or {}).get("filter", "")
                if "code=01300" in f or "pathName~AMURA" in f:
                    return {"rows": [ROW], "meta": {"size": 1}}
                return {"rows": [], "meta": {"size": 0}}
            if path == f"/entity/product/{PID}/images":
                return {"rows": [{"meta": {"href": "https://api.moysklad.ru/x/images/aaaa-1", "downloadHref": "https://ms/dl/1"}},
                                 {"meta": {"href": "https://api.moysklad.ru/x/images/bbbb-2", "downloadHref": "https://ms/dl/2"}}]}
            raise AssertionError(path)

        def get(url, **k):
            self.dl.append(url)
            return type("R", (), {"content": png(), "raise_for_status": lambda s: None})()
        for obj, name, val in ((oh, "ms", ms), (oh.S, "get", get), (ms_goods, "CACHE_DIR", tempfile.mkdtemp()),
                               (mp_calc, "cost_override", lambda pid: None), (mp_calc, "supply_cost", lambda pid: (None, "")),
                               (mp_calc, "to_kzt", lambda v, cur, *a, **k: (v * 500, "курс МиГ 500"))):
            old = getattr(obj, name)
            setattr(obj, name, val)
            self.addCleanup(lambda o=obj, n=name, v=old: setattr(o, n, v))
        ms_goods._imgs.clear()
        ms_goods._codes.clear()
        ms_goods._miss.clear()
        app = Flask(__name__)
        app.register_blueprint(agent.bp)
        app.register_blueprint(ms_goods.bp)
        self.c = app.test_client()
        self.h = {"Authorization": f"Bearer {TOKEN}"}

    def read(self, p, h=None):
        return self.c.get("/agent/api/read/ms/products", query_string={"p": json.dumps(p)}, headers=self.h if h is None else h)

    def test_by_codes(self):
        j = self.read({"codes": ["01300", "99999"]}).get_json()
        self.assertTrue(j["ok"], j)
        self.assertEqual(j["missing"], ["99999"])
        it = j["items"][0]
        self.assertEqual((it["code"], it["name"], it["description"], it["country"], it["group"]), ("01300", "Крем AMURA", "Увлажняющий", "Корея", "Бренды/AMURA"))
        self.assertEqual(it["barcodes"], [{"type": "ean13", "value": "4600000000017"}])
        self.assertEqual((it["cost"], it["buy"]), (6250, 12.5))                       # 12.5 без валюты — доллары по МиГ
        self.assertIn("USD", it["costNote"])
        self.assertEqual((it["weight_kg"], it["volume_l"], it["w_cm"], it["h_cm"], it["d_cm"]), (0.2, 0.25, 5, 12, 5))
        self.assertEqual(it["photos"], ["https://admin.amura.kz/pub/msimg/01300/1.jpg", "https://admin.amura.kz/pub/msimg/01300/2.jpg"])

    def test_by_folder_and_limits(self):
        j = self.read({"folder": "AMURA", "photos": False}).get_json()
        self.assertEqual(([x["code"] for x in j["items"]], j["next"]), (["01300"], None))
        self.assertNotIn("photos", j["items"][0])
        self.assertTrue(any("archived=false" in (p or {}).get("filter", "") for _, p in self.ms_calls))
        self.assertEqual(self.read({}).status_code, 400)
        self.assertEqual(self.read({"codes": [str(i) for i in range(51)]}).status_code, 400)
        self.assertEqual(self.read({"codes": ["01300"]}, h={}).status_code, 401)          # без токена агента — нет

    def test_public_photo(self):
        self.patch_codes()
        r = self.c.get("/pub/msimg/01300/2.jpg")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/jpeg"))
        im = Image.open(io.BytesIO(r.data))
        self.assertEqual((im.format, im.size, im.getpixel((0, 0))), ("JPEG", (40, 30), (255, 255, 255)))   # прозрачное — белым
        self.assertEqual(self.dl, ["https://ms/dl/2"])
        self.assertEqual(self.c.get("/pub/msimg/01300/2.jpg").status_code, 200)
        self.assertEqual(self.dl, ["https://ms/dl/2"])                                     # второй раз — из кэша на диске
        for bad in ("/pub/msimg/01300/3.jpg", "/pub/msimg/01300/0.jpg", "/pub/msimg/nope/1.jpg", "/pub/msimg/..%2Fx/1.jpg",
                    "/pub/msimg/01300/1.png"):
            self.assertEqual(self.c.get(bad).status_code, 404, bad)

    def test_miss_limit(self):
        self.patch_codes()
        ms_goods._miss.extend([__import__("time").time()] * ms_goods.MISS_PER_MIN)
        self.assertEqual(self.c.get("/pub/msimg/01300/1.jpg").status_code, 503)

    def patch_codes(self):
        old = mpk.ms_by_codes
        mpk.ms_by_codes = lambda codes: {"01300": PID} if "01300" in codes else {}
        self.addCleanup(lambda: setattr(mpk, "ms_by_codes", old))


if __name__ == "__main__":
    unittest.main()
