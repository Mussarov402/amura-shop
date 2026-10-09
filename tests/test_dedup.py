"""Поиск клиента по номеру: дубли не плодятся, как бы номер ни был записан в МойСклад.
Запуск: python -m unittest discover -s tests"""
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

import order_hook as oh  # noqa: E402
import inbox  # noqa: E402
import mirror  # noqa: E402


class FakeMS:
    """МойСклад: search ищет подстрокой по имени и номеру как он записан."""
    def __init__(self, agents):
        self.agents, self.posts, self.searches = agents, [], []

    def __call__(self, method, path, params=None, json=None, **k):
        if method == "GET" and path == "/entity/counterparty":
            q = params["search"]
            self.searches.append(q)
            return {"rows": [a for a in self.agents if q in a.get("phone", "") or q in a["name"]][:params.get("limit", 25)]}
        if method == "GET" and path.startswith("/entity/counterparty/"):
            return next(a for a in self.agents if a["id"] == path.rsplit("/", 1)[1])
        if method == "POST" and path == "/entity/counterparty":
            self.posts.append(json)
            return {"id": "new", **json}
        if method == "PUT":
            return {}
        raise AssertionError(path)


class DedupTest(unittest.TestCase):
    def setUp(self):
        with mirror.db() as d:
            d.run("DELETE FROM ms_agent")
            inbox.set_setting(d, mirror.FLAG, "0")

    def use(self, agents):
        oh.ms = self.ms = FakeMS(agents)

    def test_formatted_phone_found(self):
        for ph in ("+7 (701) 234-56-78", "8 701 234 56 78", "87012345678", "+77012345678", "701-234-56-78"):
            self.use([{"id": "a1", "name": "Айгуль", "phone": ph}])
            r = oh.cp_by_phone("77012345678")
            self.assertEqual(r and r["id"], "a1", ph)

    def test_other_number_not_matched(self):
        self.use([{"id": "a1", "name": "Айгуль", "phone": "+7 (701) 234-56-79"}])
        self.assertIsNone(oh.cp_by_phone("77012345678"))

    def test_prefers_active_and_oldest(self):
        self.use([{"id": "new", "name": "Новый клиент сайта", "phone": "+77012345678", "created": "2026-10-01"},
                  {"id": "old", "name": "Айгуль", "phone": "+77012345678", "created": "2025-01-01"},
                  {"id": "arc", "name": "Старая", "phone": "+77012345678", "created": "2024-01-01", "archived": True}])
        self.assertEqual(oh.cp_by_phone("+7 701 234 5678")["id"], "old")

    def test_mirror_first(self):
        mirror.set_enabled(True)
        with mirror.db() as d:
            d.run("INSERT INTO ms_agent (id, name, phone, deleted, archived) VALUES ('m1', 'Айгуль', '8(701)234 56 78', 0, 0)")
        self.use([{"id": "m1", "name": "Айгуль", "phone": "8(701)234 56 78"}])
        self.assertEqual(oh.cp_by_phone("77012345678")["id"], "m1")
        self.assertEqual(self.ms.searches, [])          # нашли в копии — МойСклад не перебирали

    def test_order_agent_reuses_existing(self):
        self.use([{"id": "a1", "name": "Айгуль", "phone": "+7 (701) 234-56-78"}])
        self.assertEqual(oh._find_or_create_agent("Айгуль", "77012345678", "", "Алматы"), "a1")
        self.assertEqual(self.ms.posts, [])

    def test_sms_verify_no_duplicate(self):
        import hashlib
        import time
        from flask import Flask
        self.use([{"id": "a1", "name": "Айгуль", "phone": "+7 (701) 234-56-78", "tags": ["сайт"]}])
        oh._codes["77012345678"] = {"t": time.time(), "tries": 0, "h": hashlib.sha256(b"123477012345678").hexdigest()}
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        orig = oh.profile
        oh.profile = lambda cp: {"city": "Алматы"}
        try:
            r = app.test_client().post("/auth/sms/verify", json={"phone": "+7 701 234 56 78", "code": "1234"})
        finally:
            oh.profile = orig
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self.ms.posts, [])

    def test_mirror_prefers_oldest(self):
        mirror.set_enabled(True)
        with mirror.db() as d:
            d.run("INSERT INTO ms_agent (id, name, phone, deleted, archived) VALUES ('new', 'Kamila', '77012345678', 0, 0)")
            d.run("INSERT INTO ms_agent (id, name, phone, deleted, archived) VALUES ('old', 'Камила', '+77012345678', 0, 0)")
        self.use([{"id": "new", "name": "Kamila", "phone": "77012345678", "created": "2026-03-07"},
                  {"id": "old", "name": "Камила", "phone": "+77012345678", "created": "2025-10-25"}])
        self.assertEqual(oh.cp_by_phone("77012345678")["id"], "old")

    def test_merged_duplicate_redirects(self):
        main = "11111111-2222-3333-4444-555555555555"
        self.use([{"id": "dup", "name": "Kamila", "phone": "+77012345678", "archived": True,
                   "description": f"Дубль → Камила Днг ({main})\nКлиент с сайта"},
                  {"id": "solo", "name": "Айгуль", "phone": "+77019999999"}])
        oh._live.clear()
        self.assertEqual(oh.live_cid("dup"), main)
        self.assertEqual(oh.live_cid("solo"), "solo")

    def test_session_token_follows_merge(self):
        from flask import Flask
        main = "11111111-2222-3333-4444-555555555555"
        self.use([{"id": "dup", "name": "Kamila", "archived": True, "description": f"Дубль → Камила ({main})"}])
        oh._live.clear()
        app = Flask(__name__)
        with app.test_request_context(headers={"Authorization": "Bearer " + oh.make_token("dup")}):
            self.assertEqual(oh.session_cid(), main)


if __name__ == "__main__":
    unittest.main()
