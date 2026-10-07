"""Тесты зеркала МойСклад: SQLite и подменённый oh.ms (сеть не нужна).
Запуск: python -m unittest discover -s tests"""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ["INBOX_SQLITE"] = os.path.join(tempfile.mkdtemp(), "t.db")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "fonts"), exist_ok=True)       # шрифты PDF (папка fonts/ в .gitignore, на сервере её собирает сборка)
for _f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
    if not os.path.exists(os.path.join(ROOT, "fonts", _f)):
        import shutil
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

import order_hook as oh  # noqa: E402
oh.ms = lambda *a, **k: {}          # фоновые потоки order_hook не ходят в сеть
import inbox  # noqa: E402
import mirror  # noqa: E402

STORE_A, STORE_B = "s-a", "s-b"


def prod(i, upd, price=1000, archived=False):
    return {"id": f"p{i}", "name": f"Крем {i}", "code": f"{i:05d}", "updated": upd, "archived": archived, "pathName": "Бренд",
            "salePrices": [{"value": price * 100, "priceType": {"name": "Оптовая цена"}}, {"value": price * 150, "priceType": {"name": "Розничная цена"}}],
            "buyPrice": {"value": 50000}, "barcodes": [{"ean13": f"460{i:010d}"}]}


class FakeMS:
    def __init__(self):
        self.products = []
        self.variants = []
        self.agents = []
        self.calls = []
        self.stock = {}       # (pid, store) -> qty
        self.reserve = {}

    def __call__(self, method, path, params=None, **kw):
        assert method == "GET", "зеркало не должно писать в МойСклад"
        params = params or {}
        self.calls.append((path, dict(params)))
        if path in ("/entity/product", "/entity/variant", "/entity/counterparty"):
            rows = {"/entity/product": self.products, "/entity/variant": self.variants, "/entity/counterparty": self.agents}[path]
            flt = params.get("filter", "")
            if flt.startswith("updated>="):
                rows = [r for r in rows if r["updated"][:19] >= flt[9:]]
            elif flt == "archived=false":
                rows = [r for r in rows if not r["archived"]]
            rows = sorted(rows, key=lambda r: r["updated"])
            off, lim = params.get("offset", 0), params.get("limit", 1000)
            return {"meta": {"size": len(rows)}, "rows": rows[off:off + lim]}
        if path == "/entity/store":
            return {"rows": [{"id": STORE_A, "name": "Основной склад"}, {"id": STORE_B, "name": "Точка"}]}
        if path == "/report/stock/bystore/current":
            src = self.stock if params["stockType"] == "stock" else self.reserve
            return [{"assortmentId": p, "storeId": s, params["stockType"]: q} for (p, s), q in src.items()]
        if path == "/report/stock/all/current":
            tot = {}
            for (p, _), q in self.stock.items():
                tot[p] = tot.get(p, 0) + q
            return [{"assortmentId": p, "stock": q} for p, q in tot.items()]
        raise AssertionError(path)


class MirrorTest(unittest.TestCase):
    def setUp(self):
        with mirror.db() as d:
            for t in ("ms_product", "ms_agent", "ms_store", "ms_stock", "ms_sync", "ms_recon"):
                d.run(f"DELETE FROM {t}")
            inbox.set_setting(d, mirror.FLAG, "0")
        self.ms = FakeMS()
        oh.ms = self.ms
        mirror.PAGE = 3
        oh._cache.pop("live", None)

    def count(self, sql, args=()):
        with mirror.db() as d:
            return d.run(sql, args, one=True)[0]

    def test_flag_default_off(self):
        self.assertFalse(mirror.enabled())
        mirror.set_enabled(True)
        self.assertTrue(mirror.enabled())

    def test_initial_load_in_parts_and_same_timestamp(self):
        # 8 товаров, 5 с одинаковым updated — пагинация по ключу не теряет и не дублирует строки
        self.ms.products = [prod(i, "2026-10-01 10:00:00.000") for i in range(5)] + [prod(i, f"2026-10-0{i - 3} 11:00:00.000") for i in range(5, 8)]
        n, caught = mirror.sync_entity("product", max_pages=1, pause=0)
        self.assertEqual((n, caught), (3, False))
        n, caught = mirror.sync_entity("product", max_pages=10, pause=0)
        self.assertTrue(caught)
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_product"), 8)
        with mirror.db() as d:
            pr = d.run("SELECT prices, buy_price, barcodes FROM ms_product WHERE id='p1'", one=True)
        self.assertIn('"Оптовая цена": 1000', pr[0])
        self.assertEqual(pr[1], 500)
        self.assertIn("4600000000001", pr[2])

    def test_incremental_picks_changes_only(self):
        self.ms.products = [prod(i, f"2026-10-01 10:00:0{i}.000") for i in range(4)]
        mirror.sync_entity("product", pause=0)
        self.ms.calls.clear()
        self.ms.products[1] = prod(1, "2026-10-02 09:00:00.000", price=2000)
        mirror.sync_entity("product", pause=0)
        self.assertTrue(self.ms.calls[0][1]["filter"].startswith("updated>=2026-10-01 10:00:03"))
        with mirror.db() as d:
            self.assertIn("2000", d.run("SELECT prices FROM ms_product WHERE id='p1'", one=True)[0])

    def test_full_pass_marks_deleted(self):
        self.ms.products = [prod(i, f"2026-10-01 10:00:0{i}.000") for i in range(4)]
        mirror.sync_entity("product", pause=0)
        del self.ms.products[2]
        with mirror.db() as d:                       # «прошли сутки» — следующий проход полный
            d.run("UPDATE ms_sync SET full_done=1 WHERE entity='product'")
        mirror.sync_entity("product", pause=0)
        self.assertEqual(self.count("SELECT deleted FROM ms_product WHERE id='p2'"), 1)
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_product WHERE deleted=0"), 3)

    def test_tick_stock_and_reconcile(self):
        self.ms.products = [prod(i, f"2026-10-01 10:00:0{i}.000") for i in range(4)] + [prod(9, "2026-10-01 10:00:09.000", archived=True)]
        self.ms.variants = [{**prod(20, "2026-10-01 12:00:00.000"), "product": {"meta": {"href": "https://x/entity/product/p1"}}}]
        self.ms.stock = {("p0", STORE_A): 5, ("p0", STORE_B): 2, ("p1", STORE_A): 7.5}
        self.ms.reserve = {("p0", STORE_A): 1}
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(mirror.tick())
        self.assertIn("Зеркало МойСклад, проход: store 2; product +5", out.getvalue())
        self.assertIn("догнали: да", out.getvalue())
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_store"), 2)
        self.assertEqual(self.count("SELECT reserve FROM ms_stock WHERE product_id='p0' AND store_id=%s", (STORE_A,)), 1)
        self.assertEqual(self.count("SELECT parent_id FROM ms_product WHERE id='p20'"), "p1")
        oh._cache["live"] = (0, {"items": [{"id": "p0", "name": "Крем 0", "opt": 1000, "rtl": 1500}, {"id": "p3", "name": "Крем 3", "opt": 999, "rtl": 1500}]})
        r = mirror.reconcile()
        by = {c["name"]: c for c in r["checks"]}
        self.assertTrue(by["Товары (не в архиве)"]["ok"])
        self.assertEqual(by["Товары (не в архиве)"]["ours"], 4)
        self.assertTrue(by["Модификации (не в архиве)"]["ok"])
        self.assertTrue(by["Остаток, всего единиц"]["ok"])
        self.assertEqual(by["Цены товаров сайта"]["ours"], 1)
        self.assertFalse(r["ok"])
        # остаток в МойСклад изменился — сверка это видит до следующей синхронизации
        self.ms.stock[("p1", STORE_A)] = 3
        by = {c["name"]: c for c in mirror.reconcile()["checks"]}
        self.assertEqual(by["Товары с другим остатком"]["ours"], 1)
        st = mirror.status()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["entities"]["product"]["rows"], 5)
        self.assertIsNotNone(st["recon"])
        self.assertEqual(mirror.stock_of("Крем 0")[0]["id"], "p0")
        items = mirror.stock_of("00000")
        self.assertEqual(len(items[0]["stores"]), 2)

    def test_counterparties(self):
        self.ms.agents = [{"id": f"a{i}", "name": f"ИП Клиент {i}", "phone": f"+7700000000{i}", "companyType": "entrepreneur",
                           "tags": ["опт"] if i % 2 else [], "updated": f"2026-10-01 10:00:0{i}.000", "archived": i == 4} for i in range(5)]
        n, caught = mirror.sync_entity("counterparty", pause=0)
        self.assertEqual((n, caught), (5, True))
        with mirror.db() as d:
            r = d.run("SELECT name, phone, company_type, tags FROM ms_agent WHERE id='a1'", one=True)
        self.assertEqual(r[:3], ("ИП Клиент 1", "+77000000001", "entrepreneur"))
        self.assertIn("опт", r[3])
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_product"), 0)   # товары не задеты
        del self.ms.agents[0]
        with mirror.db() as d:
            d.run("UPDATE ms_sync SET full_done=1 WHERE entity='counterparty'")
        mirror.sync_entity("counterparty", pause=0)
        self.assertEqual(self.count("SELECT deleted FROM ms_agent WHERE id='a0'"), 1)
        self.assertEqual(mirror.status()["entities"]["counterparty"]["rows"], 4)
        by = {c["name"]: c for c in mirror.reconcile()["checks"]}
        self.assertEqual(by["Контрагенты (не в архиве)"]["ours"], 3)
        self.assertTrue(by["Контрагенты (не в архиве)"]["ok"])
        self.assertGreater(mirror.last_recon(), 0)
        import contextlib, io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mirror.log_last_recon()
        self.assertIn("Зеркало МойСклад, сверка (последняя,", out.getvalue())
        self.assertIn("Контрагенты (не в архиве): 3/3", out.getvalue())

    def test_site_room_waits_for_free_slots(self):
        import threading
        sem = threading.BoundedSemaphore(4)
        old, oh.MS_PARALLEL = oh.MS_PARALLEL, sem
        try:
            for _ in range(3):
                sem.acquire()
            t0 = mirror.time.time()
            mirror._site_room(wait=1)                # сайту осталось одно место — зеркало ждёт
            self.assertGreaterEqual(mirror.time.time() - t0, 0.9)
            sem.release()
            t0 = mirror.time.time()
            mirror._site_room(wait=1)
            self.assertLess(mirror.time.time() - t0, 0.2)
        finally:
            oh.MS_PARALLEL = old

    def test_error_recorded_not_raised(self):
        def boom(*a, **k):
            raise RuntimeError("МойСклад 503")
        oh.ms = boom
        self.assertFalse(mirror.tick())
        self.assertIn("503", mirror.status()["entities"]["product"]["error"])


class AdminApiTest(unittest.TestCase):
    def test_owner_only(self):
        import app
        c = app.app.test_client()
        self.assertEqual(c.get("/admin/api/mirror").status_code, 401)
        oh.ORDER_SECRET = b"x"
        import admin
        tok = admin.make_admin_token()
        oh.ms = FakeMS()
        r = c.get("/admin/api/mirror", headers={"Authorization": "Bearer " + tok})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["enabled"])


if __name__ == "__main__":
    unittest.main()
