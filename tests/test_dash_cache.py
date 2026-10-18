"""Дашборд «Деньги»: прошлый период (уже не меняется) берётся из кэша — МойСклад спрашиваем только про текущий;
повторный показ — сразу из кэша (свежие данные в фоне)."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin  # noqa: E402
import order_hook as oh  # noqa: E402


class DashCache(unittest.TestCase):
    def test_prev_period_cached(self):
        calls = []
        saved = (admin._ms_all, admin._profit_rows)
        self.addCleanup(lambda: (setattr(admin, "_ms_all", saved[0]), setattr(admin, "_profit_rows", saved[1])))
        admin._ms_all = lambda path, flt, **k: calls.append(("all", path, flt)) or []
        admin._profit_rows = lambda a, b: calls.append(("prof", a, b)) or []
        oh._cache["adm_order_states"] = (9e18, {"states": []})
        self.addCleanup(lambda: [oh._cache.pop(k) for k in list(oh._cache) if k.startswith(("dq:", "dp:", "adm_order_states"))])
        today = datetime.now(oh.ALMATY).strftime("%Y-%m-%d")
        week = (datetime.now(oh.ALMATY) - timedelta(days=7)).strftime("%Y-%m-%d")
        admin._dash_calc(today, today, week, week)
        first = len(calls)
        self.assertEqual(first, 12)                                  # 6 запросов за сегодня + 6 за прошлый период
        admin._dash_calc(today, today, week, week)
        self.assertEqual(len(calls) - first, 0)                       # минута не прошла — всё из кэша
        for k, v in list(oh._cache.items()):                         # прошла минута: всё, что в кэше, «состарилось» на 2 минуты
            if k.startswith(("dq:", "dp:")):
                oh._cache[k] = (v[0] - 120, v[1])
        admin._dash_calc(today, today, week, week)
        again = calls[first:]
        self.assertEqual(len(again), 6)                               # снова — только сегодняшние 6, прошлый период ещё в кэше (час)
        firsts = set(map(str, calls[:first]))
        self.assertTrue(set(map(str, again)) <= firsts)                 # те же запросы про сегодня
        self.assertEqual(len(firsts - set(map(str, again))), 6)        # 6 запросов про прошлый период не повторялись


if __name__ == "__main__":
    unittest.main()
