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
        delivery.save({"rules": {"city_price": "990", "city_free": "5000", "kz_price": 1990, "kz_free": 15000}})
        self.assertEqual(delivery.price(4500, "city"), 990)
        self.assertEqual(delivery.price(5000, "city"), 0)
        self.assertEqual(delivery.price(12000, "kz"), 1990)
        self.assertEqual(delivery.price(15000, "kz"), 0)
        delivery.save({"rules": {"city_price": "abc"}})                # мусор не ломает настройки
        self.assertEqual(delivery.price(100, "city"), 990)

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
