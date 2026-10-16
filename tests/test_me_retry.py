"""Профиль клиента (PATCH /me): 412 [3007] от МойСклад — один повтор, затем понятный 409 вместо 500."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401  (общая настройка: SQLite, шрифты)
import app  # noqa: E402
import order_hook as oh  # noqa: E402

CONFLICT = RuntimeError('МойСклад 412: {"errors":[{"error":"Документ был изменен другим пользователем","code":3007}]}')


class MeRetryTest(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        self.puts, self.gets, self.fail = [], 0, []
        self._ms, self._sleep, self._notif = oh.ms, oh.time.sleep, oh.notif_on
        oh.time.sleep = lambda s: None
        oh.notif_on = lambda k: False

        def ms(method, path, params=None, json=None, **kw):
            if path == "/entity/counterparty/c1":
                if method == "GET":
                    self.gets += 1
                    return {"id": "c1", "name": "Айгерим (Алматы)", "phone": "+77011234567", "actualAddress": "Алматы"}
                if method == "PUT":
                    if self.fail:
                        raise self.fail.pop(0)
                    self.puts.append(json)
                    return {}
            if path == "/entity/customerorder":
                return {"rows": []}
            raise AssertionError((method, path))
        oh.ms = ms
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + oh.make_token("c1")}

    def tearDown(self):
        oh.ms, oh.time.sleep, oh.notif_on = self._ms, self._sleep, self._notif

    def patch(self):
        return self.c.patch("/me", headers=self.h, json={"name": "Айгерим", "city": "Астана"})

    def test_second_try_succeeds(self):
        self.fail = [CONFLICT]
        r = self.patch()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.puts, [{"name": "Айгерим (Астана)", "actualAddress": "Астана"}])
        self.assertGreaterEqual(self.gets, 2)                          # перед повтором контрагент перечитан

    def test_conflict_twice_gives_409(self):
        self.fail = [CONFLICT, CONFLICT]
        r = self.patch()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["error"], "Не удалось сохранить, попробуйте ещё раз")
        self.assertEqual(self.puts, [])

    def test_other_errors_unchanged(self):
        self.fail = [RuntimeError("МойСклад 400: что-то другое"), CONFLICT]
        r = self.patch()
        self.assertEqual(r.status_code, 500)                           # как раньше: другие ошибки не перехватываем и не повторяем
        self.assertEqual(len(self.fail), 1)


if __name__ == "__main__":
    unittest.main()
