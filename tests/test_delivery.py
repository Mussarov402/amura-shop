"""Модуль «Доставка»: настройки, секреты не уходят в панель, цена для клиента, проверка подключения (сеть подменена)."""
import json
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
import delivery  # noqa: E402


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, json.dumps(body)

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class DeliveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with inbox.db() as d:
            d.run("CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT)")

    def test_secrets_hidden_and_kept(self):
        delivery.save({"yandex": {"on": True, "token": "y-secret-ABCD"}, "cdek": {"client_id": "acc1", "secret": "c-secret-WXYZ"}})
        c = delivery.public_conf()
        dump = json.dumps(c, ensure_ascii=False)
        self.assertNotIn("y-secret-ABCD", dump)
        self.assertNotIn("c-secret-WXYZ", dump)
        self.assertIn("acc1", dump)                                   # аккаунт СДЭК — не секрет
        self.assertTrue(c["services"]["yandex"]["on"])
        self.assertEqual(c["services"]["yandex"]["fields"][0]["tail"], "ABCD")
        delivery.save({"yandex": {"token": ""}})                       # пустое поле — ключ не стирается
        self.assertEqual(delivery.conf()["svc"]["yandex"]["token"], "y-secret-ABCD")

    def test_price_rules(self):
        delivery.save({"rules": {"free_from": "20000"}, "tiers": [[15000, 500], [5000, 1500], [20000, 500], ["x", 1]]})
        self.assertEqual(delivery.conf()["tiers"], [[5000, 1500], [15000, 500], [20000, 500]])   # по возрастанию, мусор отброшен
        self.assertEqual(delivery.price(3000), 1500)
        self.assertEqual(delivery.price(4999, "cdek"), 1500)
        self.assertEqual(delivery.price(5000), 500)
        self.assertEqual(delivery.price(17000), 500)
        self.assertEqual(delivery.price(20000), 0)
        self.assertEqual(delivery.price(3000, "pickup"), 0)
        delivery.save({"rules": {"free_from": 0}})                     # 0 — бесплатной нет: выше последней ступени — её цена
        self.assertEqual(delivery.price(50000), 500)
        delivery.save({"rules": {"free_from": "abc"}})                 # мусор не ломает настройки
        self.assertEqual(delivery.price(100), 1500)

    def test_schedule_and_slots(self):
        from datetime import datetime
        delivery.save({"rules": {"cutoff": 90}, "schedule": {"days": "0111111", "open": "08:00", "close": "18:00", "slots": [
            {"name": "После обеда", "from": "14:00", "to": "18:00"}, {"name": "Утро", "from": "08:00", "to": "11:00"},
            {"name": "Обед", "from": "11:00", "to": "14:00"}, {"name": "", "from": "18:00", "to": "20:00"},
            {"name": "Кривой", "from": "15:00", "to": "12:00"}]}})
        sc = delivery.conf()["schedule"]
        self.assertEqual([x["name"] for x in sc["slots"]], ["Утро", "Обед", "После обеда"])   # пустые и кривые отброшены, по времени
        # воскресенье 11.10.2026, 12:00: сегодня — только «После обеда» (до 14:00 больше 90 мин), понедельник — выходной
        got = delivery.slots_ahead(datetime(2026, 10, 11, 12, 0), days=2)
        self.assertEqual([(x["date"], x["name"]) for x in got[:2]], [("2026-10-11", "После обеда"), ("2026-10-13", "Утро")])
        self.assertNotIn("2026-10-12", {x["date"] for x in got})
        got = delivery.slots_ahead(datetime(2026, 10, 11, 12, 45), days=1)   # до 14:00 меньше 90 мин — сегодня уже нельзя
        self.assertEqual(got[0]["date"], "2026-10-13")

    def test_ensure_dims(self):
        import admin
        made = []
        with mock.patch.object(admin, "_attr", side_effect=lambda e, n, t: made.append((e, n, t))):
            delivery.ensure_dims()
        self.assertEqual(made, [("product", "Ширина, см", "double"), ("product", "Высота, см", "double"), ("product", "Глубина, см", "double")])

    def test_yandex_test(self):
        delivery.save({"yandex": {"token": "tok"}})
        with mock.patch.object(delivery.requests, "post", return_value=Resp(200, {"claims": []})) as p:
            self.assertEqual(delivery.test("yandex"), "ключ принят")
            self.assertEqual(p.call_args.kwargs["headers"]["Authorization"], "Bearer tok")
        with mock.patch.object(delivery.requests, "post", return_value=Resp(401, {})):
            with self.assertRaisesRegex(RuntimeError, "не принял ключ"):
                delivery.test("yandex")

    def test_cdek_test(self):
        delivery.save({"cdek": {"client_id": "id", "secret": "sec", "test": True}})
        with mock.patch.object(delivery.requests, "post", return_value=Resp(200, {"access_token": "T"})) as p, \
                mock.patch.object(delivery.requests, "get", return_value=Resp(200, [{"city": "Алматы", "code": 4756}])) as g:
            self.assertIn("Алматы", delivery.test("cdek"))
            self.assertTrue(p.call_args.args[0].startswith("https://api.edu.cdek.ru"))   # тестовый режим — учебный сервер
            self.assertEqual(g.call_args.kwargs["headers"]["Authorization"], "Bearer T")
        with mock.patch.object(delivery.requests, "post", return_value=Resp(401, {})):
            with self.assertRaisesRegex(RuntimeError, "не принял"):
                delivery.test("cdek")

    def test_unknown_service(self):
        with self.assertRaises(ValueError):
            delivery.test("dhl")


if __name__ == "__main__":
    unittest.main()
