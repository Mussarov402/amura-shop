"""Прогрев после запуска сервера: заказы, статусы, «Сегодня» и каталог грузятся заранее; сбой МойСклад не роняет поток."""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin  # noqa: E402
import order_hook as oh  # noqa: E402


class Warm(unittest.TestCase):
    def test_warm_fills_cache(self):
        saved = {n: getattr(admin, n) for n in ("_ms_states", "_ms_orders", "_ms_today", "_ms_ping")}
        old_refresh, old_sleep = oh.refresh, admin.time.sleep
        self.addCleanup(lambda: [setattr(admin, n, v) for n, v in saved.items()] + [setattr(oh, "refresh", old_refresh), setattr(admin.time, "sleep", old_sleep)])
        admin.time.sleep = lambda s: None
        admin._ms_states = lambda: {"states": []}
        admin._ms_orders = lambda: [{"name": "1"}]
        admin._ms_today = lambda: (_ for _ in ()).throw(RuntimeError("МойСклад не отвечает"))     # сбой — остальное всё равно греется
        admin._ms_ping = lambda: {"ok": True, "sec": 0.1}
        called = []
        oh.refresh = lambda ttl: called.append(ttl)
        for k in ("adm_order_states", "adm_orders", "adm_today", "adm_ping"):
            oh._cache.pop(k, None)
        admin.warm()
        self.assertEqual(oh._cache["adm_orders"][1], [{"name": "1"}])
        self.assertIn("adm_order_states", oh._cache)
        self.assertNotIn("adm_today", oh._cache)
        self.assertEqual(called, [oh.LIVE_TTL])
        for k in ("adm_order_states", "adm_orders", "adm_ping"):
            oh._cache.pop(k, None)


if __name__ == "__main__":
    unittest.main()
