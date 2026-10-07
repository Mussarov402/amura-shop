"""Расчёт реальной стоимости доставки: разбор заказов, статистика, полный проход с подменённой сетью."""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "fonts"), exist_ok=True)
for _f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
    if not os.path.exists(os.path.join(ROOT, "fonts", _f)):
        import shutil
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

import inbox  # noqa: E402
import order_hook as oh  # noqa: E402
import delivery  # noqa: E402
import delivery_estimate as de  # noqa: E402


class Resp:
    def __init__(self, body, code=200):
        self._b, self.status_code, self.ok, self.text = body, code, code < 400, str(body)

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


ORDERS = [
    {"description": "Заказ с сайта\nТомирис, WhatsApp +7707\nГород: Алматы\nОтправка: Курьер по городу — Сейфуллина 597, кв 7"},
    {"description": "Заказ с сайта\nАлия, WhatsApp +7705\nГород: Караганда\nОтправка: КАМАЗ — Коля Галя"},
    {"description": "Заказ с сайта\nДиана, WhatsApp +7778\nГород: караганда\nОтправка: КАМАЗ — ???"},
    {"description": "Заказ с сайта\nНасиба\nГород: Алматы\nОтправка: Самовывоз"},
    {"description": "Онлайн-заказ без формата"},
]


class EstimateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with inbox.db() as d:
            d.run("CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT)")

    def test_parse(self):
        self.assertEqual(de.parse(ORDERS[0]["description"]), ("Алматы", "Курьер по городу", "Сейфуллина 597, кв 7"))
        self.assertEqual(de.parse(ORDERS[3]["description"])[1], "Самовывоз")
        self.assertEqual(de.parse("мусор"), ("", "", ""))
        a = de._clean_addr("Алтын орда 6/33к1 67 блок 2 подез 3 этаж 22 квартира")
        self.assertTrue(a.startswith("Алтын орда 6/33к1 67"))
        self.assertNotIn("этаж", a)

    def test_stats(self):
        s = de._stats([1000, 2000, 3000], [1, 1, 2])
        self.assertEqual((s["n"], s["orders"], s["med"], s["max"]), (3, 4, 2500, 3000))
        self.assertIsNone(de._stats([]))

    def test_run(self):
        delivery.save({"yandex": {"token": "t"}, "cdek": {"client_id": "id", "secret": "s"}, "store": {"wh_addr": "Райымбека 221"}})
        oh.ms = lambda *a, **k: {"rows": ORDERS}
        de.time.sleep = lambda s: None

        def get(url, params=None, **k):
            if "nominatim" in url:
                return Resp([{"lon": "76.9", "lat": "43.2"}])
            if "/location/cities" in url:
                return Resp([{"code": 4756 if params["city"] == "Алматы" else 1000}])
            raise AssertionError(url)

        def post(url, json=None, **k):
            if "check-price" in url:
                return Resp({"price": "1450.00" if json["items"][0]["weight"] < 1 else "1650.00", "currency_rules": {"code": "KZT"}})
            if "oauth/token" in url:
                return Resp({"access_token": "T"})
            if "tarifflist" in url:
                return Resp({"tariff_codes": [{"delivery_mode": 4, "delivery_sum": 1200}, {"delivery_mode": 7, "delivery_sum": 1100},
                                              {"delivery_mode": 3, "delivery_sum": 1800}, {"delivery_mode": 1, "delivery_sum": 2300}]})
            raise AssertionError(url)

        with mock.patch.object(de.requests, "get", side_effect=get), mock.patch.object(de.requests, "post", side_effect=post), \
                mock.patch.object(delivery.requests, "post", side_effect=post):
            r = de.run()
        self.assertEqual(r["yandex"]["0,5 кг"]["med"], 1450)
        self.assertEqual(r["yandex"]["1,5 кг"]["med"], 1650)
        self.assertEqual(r["cdek_pvz"]["0,5 кг"]["med"], 1100)            # минимальный из пункт/постамат
        self.assertEqual(r["cdek_door"]["0,5 кг"]["med"], 1800)           # минимальный до двери
        self.assertEqual(r["cdek_pvz"]["0,5 кг"]["orders"], 2)            # Караганда — 2 заказа, один город
        self.assertEqual(r["regions"], [1, 1])
        self.assertEqual(de.last()["result"]["yandex"]["0,5 кг"]["med"], 1450)


if __name__ == "__main__":
    unittest.main()
