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
        self.docs = {t: [] for t in mirror.DOC_TYPES}
        self.money = []        # строки /report/money/byaccount
        self.balances = []     # строки /report/counterparty
        self.calls = []
        self.stock = {}       # (pid, store) -> qty
        self.reserve = {}

    def __call__(self, method, path, params=None, **kw):
        assert method == "GET", "зеркало не должно писать в МойСклад"
        params = params or {}
        self.calls.append((path, dict(params)))
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "entity" and parts[1] in self.docs and parts[3] == "positions":
            doc = next(r for r in self.docs[parts[1]] if r["id"] == parts[2])
            allp = doc.get("_all_pos", [])
            off, lim = params.get("offset", 0), params.get("limit", 1000)
            return {"meta": {"size": len(allp)}, "rows": allp[off:off + lim]}
        ent = path.rsplit("/", 1)[-1]
        if path.startswith("/entity/") and (ent in ("product", "variant", "counterparty") or ent in self.docs):
            rows = {"product": self.products, "variant": self.variants, "counterparty": self.agents, **self.docs}[ent]
            for f in filter(None, params.get("filter", "").split(";")):
                if f.startswith("updated>="):
                    rows = [r for r in rows if r["updated"][:19] >= f[9:]]
                elif f == "archived=false":
                    rows = [r for r in rows if not r.get("archived")]
                elif f == "applicable=true":
                    rows = [r for r in rows if r.get("applicable")]
                elif f.startswith("moment>="):
                    rows = [r for r in rows if r["moment"][:19] >= f[8:]]
                elif f.startswith("moment<"):
                    rows = [r for r in rows if r["moment"][:19] < f[7:]]
                else:
                    raise AssertionError(f)
            rows = sorted(rows, key=lambda r: r["updated"])
            off, lim = params.get("offset", 0), params.get("limit", 1000)
            out = rows[off:off + lim]
            if ent in self.docs and params.get("expand") != "positions":   # без expand МойСклад даёт только ссылку на позиции
                out = [{**r, "positions": {"meta": {"size": len((r.get("positions") or {}).get("rows") or [])}}} for r in out]
            return {"meta": {"size": len(rows)}, "rows": out}
        if path == "/report/money/byaccount":
            return {"meta": {"size": len(self.money)}, "rows": self.money}
        if path == "/report/counterparty":
            off, lim = params.get("offset", 0), params.get("limit", 1000)
            return {"meta": {"size": len(self.balances)}, "rows": self.balances[off:off + lim]}
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
            for t in ("ms_product", "ms_agent", "ms_doc", "ms_doc_line", "ms_store", "ms_stock", "ms_sync", "ms_recon",
                      "ms_money", "ms_agent_balance", "ms_payment_link"):
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

    def test_sales_documents_and_day_check(self):
        from datetime import datetime, timedelta
        now_ms = datetime.now(mirror.MS_TZ)
        ms = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S.000")
        today_a, _ = mirror._day_bounds(str(datetime.now(oh.ALMATY).date()))
        t0 = datetime.strptime(today_a, "%Y-%m-%d %H:%M:%S").replace(tzinfo=mirror.MS_TZ)
        doc = lambda i, kind, m, s, appl=True, upd=None: {
            "id": f"{kind}{i}", "name": f"{i:05d}", "moment": ms(m), "sum": s * 100, "applicable": appl,
            "updated": upd or ms(now_ms), "agent": {"meta": {"href": "https://x/entity/counterparty/a1"}},
            "description": "Заказ с сайта\nтел. 123"}
        self.ms.docs["demand"] = [doc(1, "d", t0 + timedelta(hours=1), 10000), doc(2, "d", t0 + timedelta(hours=2), 5000, appl=False),
                                  doc(3, "d", t0 - timedelta(hours=3), 7000)]           # вчера
        self.ms.docs["retaildemand"] = [doc(1, "r", t0 + timedelta(hours=3), 2500)]
        self.ms.docs["salesreturn"] = [doc(1, "s", t0 + timedelta(hours=4), 1000)]
        self.ms.docs["customerorder"] = [doc(i, "o", t0 + timedelta(minutes=i), 100, appl=False) for i in range(3)]
        old = ms(now_ms - timedelta(days=mirror.DOC_DAYS + 5))
        self.ms.docs["retailsalesreturn"] = [doc(1, "x", now_ms - timedelta(days=100), 50, upd=old)]   # старше окна — не грузим
        for t in mirror.DOC_TYPES:
            n, caught = mirror.sync_entity(t, pause=0)
            self.assertTrue(caught, t)
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_doc WHERE type='retailsalesreturn'"), 0)
        with mirror.db() as d:
            r = d.run("SELECT number, sum, applicable, agent_id, descr FROM ms_doc WHERE id='d1'", one=True)
        self.assertEqual(r, ("00001", 10000, 1, "a1", "Заказ с сайта"))
        by = {c["name"]: c for c in mirror._sales_checks()}
        day = datetime.now(oh.ALMATY).strftime("%d.%m")
        sale = by[f"Продажи {day}: отгрузки + чеки − возвраты"]
        self.assertEqual((sale["ours"], sale["theirs"], sale["ok"]), (11500, 11500, True))
        self.assertTrue(by[f"Заказы покупателей {day}"]["ok"])
        self.assertEqual(by[f"Заказы покупателей {day}"]["ours"], 3)
        # отгрузку удалили в МойСклад — полный проход помечает её, сверка снова сходится
        del self.ms.docs["demand"][0]
        with mirror.db() as d:
            d.run("UPDATE ms_sync SET full_done=1 WHERE entity='demand'")
        mirror.sync_entity("demand", pause=0)
        self.assertEqual(self.count("SELECT deleted FROM ms_doc WHERE id='d1'"), 1)
        sale = {c["name"]: c for c in mirror._sales_checks()}[f"Продажи {day}: отгрузки + чеки − возвраты"]
        self.assertEqual((sale["ours"], sale["ok"]), (1500, True))

    def test_document_lines(self):
        from datetime import datetime, timedelta
        now_ms = datetime.now(mirror.MS_TZ)
        ms = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S.000")
        pos = lambda i, pid, q, price, disc=0: {"id": f"pos{i}", "assortment": {"meta": {"href": f"https://x/entity/product/{pid}"}},
                                                "quantity": q, "price": price * 100, "discount": disc}
        d1 = {"id": "d1", "name": "1", "moment": ms(now_ms), "updated": ms(now_ms), "sum": 2700 * 100, "applicable": True,
              "positions": {"meta": {"size": 2}, "rows": [pos(1, "p1", 2, 1000), pos(2, "p2", 1, 1000, disc=30)]}}
        big = [pos(i, "p3", 1, 10) for i in range(5)]                  # позиций больше, чем вложено, — дочитываются отдельно
        d2 = {"id": "d2", "name": "2", "moment": ms(now_ms), "updated": ms(now_ms), "sum": 60 * 100, "applicable": True,
              "positions": {"meta": {"size": 5}, "rows": big[:2]}, "_all_pos": big}
        self.ms.docs["demand"] = [d1, d2]
        mirror.sync_entity("demand", pause=0)
        self.assertTrue(any(c[1].get("expand") == "positions" and c[1]["limit"] == mirror.DOC_PAGE
                            for c in self.ms.calls if c[0] == "/entity/demand"))
        with mirror.db() as d:
            ln = d.run("SELECT product_id, qty, price, discount, sum FROM ms_doc_line WHERE doc_id='d1' ORDER BY pos_id", many=True)
        self.assertEqual([tuple(x) for x in ln], [("p1", 2, 1000, 0, 2000), ("p2", 1, 1000, 30, 700)])
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_doc_line WHERE doc_id='d2'"), 5)
        c = mirror._lines_check()
        self.assertEqual((c["ours"], c["ok"]), (1, False))             # d2: документ 60 ₸, позиции 50 ₸
        self.assertIn("demand №2", c["detail"][0])
        # документ изменили — позиции заменяются, а не дублируются
        d1["positions"] = {"meta": {"size": 1}, "rows": [pos(1, "p1", 3, 1000)]}
        d1["sum"], d1["updated"] = 3000 * 100, ms(now_ms + timedelta(seconds=5))
        mirror.sync_entity("demand", pause=0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_doc_line WHERE doc_id='d1'"), 1)

    def test_daily_full_pass_is_light(self):
        from datetime import datetime, timedelta
        now_ms = datetime.now(mirror.MS_TZ)
        ms = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S.000")
        pos = lambda i, q: {"id": f"pos{i}", "assortment": {"meta": {"href": "https://x/entity/product/p1"}}, "quantity": q, "price": 100000}
        docs = [{"id": f"d{i}", "name": str(i), "moment": ms(now_ms), "updated": ms(now_ms - timedelta(minutes=i)), "sum": 1000 * 100,
                 "applicable": True, "positions": {"meta": {"size": 1}, "rows": [pos(1, 1)]}, "_all_pos": [pos(1, 1)]} for i in range(4)]
        self.ms.docs["demand"] = docs
        mirror.sync_entity("demand", pause=0)                          # первая загрузка — с позициями
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_doc_line"), 4)
        # документ d2 изменили (позиций стало 2), но новая версия ещё не догружена; начинается суточный полный проход
        docs[2]["updated"] = ms(now_ms + timedelta(minutes=1))
        docs[2]["_all_pos"] = [pos(1, 1), pos(2, 3)]
        docs[2]["positions"] = {"meta": {"size": 2}, "rows": docs[2]["_all_pos"]}
        with mirror.db() as d:
            d.run("UPDATE ms_sync SET full_done=1 WHERE entity='demand'")
        self.ms.calls.clear()
        mirror.sync_entity("demand", pause=0)
        lists = [c[1] for c in self.ms.calls if c[0] == "/entity/demand"]
        self.assertTrue(lists and all("expand" not in p and p["limit"] == mirror.PAGE for p in lists))
        per_doc = [c[0] for c in self.ms.calls if c[0].endswith("/positions")]
        self.assertEqual(per_doc, ["/entity/demand/d2/positions"])     # дочитаны позиции только изменённого документа
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_doc_line WHERE doc_id='d2'"), 2)
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_doc_line"), 5)

    def test_stock_documents(self):
        from datetime import datetime, timedelta
        now_ms = datetime.now(mirror.MS_TZ)
        ms = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S.000")
        st = lambda sid: {"meta": {"href": f"https://x/entity/store/{sid}"}}
        pos = {"id": "pos1", "assortment": {"meta": {"href": "https://x/entity/product/p1"}}, "quantity": 4, "price": 50000}
        self.ms.docs["move"] = [{"id": "m1", "name": "00001", "moment": ms(now_ms - timedelta(days=1)), "updated": ms(now_ms),
                                 "sum": 2000 * 100, "applicable": True, "sourceStore": st(STORE_A), "targetStore": st(STORE_B),
                                 "positions": {"meta": {"size": 1}, "rows": [pos]}}]
        self.ms.docs["supply"] = [{"id": "s1", "name": "00007", "moment": ms(now_ms - timedelta(days=d)), "updated": ms(now_ms),
                                   "sum": 100, "applicable": True, "store": st(STORE_A), "agent": {"meta": {"href": "https://x/entity/counterparty/a9"}}}
                                  for d in (2,)] + [{"id": "s2", "name": "00008", "moment": ms(now_ms - timedelta(days=20)),
                                                     "updated": ms(now_ms), "sum": 100, "applicable": True, "store": st(STORE_A)}]
        for t in mirror.STOCK_DOCS:
            self.assertTrue(mirror.sync_entity(t, pause=0)[1], t)
        with mirror.db() as d:
            r = d.run("SELECT type, store_id, store2_id FROM ms_doc WHERE id='m1'", one=True)
            self.assertEqual(tuple(r), ("move", STORE_A, STORE_B))
            self.assertEqual(d.run("SELECT agent_id, store_id FROM ms_doc WHERE id='s1'", one=True)[0], "a9")
        self.assertEqual(self.count("SELECT qty FROM ms_doc_line WHERE doc_id='m1'"), 4)
        by = {c["name"]: c for c in mirror._stock_doc_checks()}
        self.assertEqual((by["Перемещения за 7 дней"]["ours"], by["Перемещения за 7 дней"]["ok"]), (1, True))
        self.assertEqual((by["Приёмки за 7 дней"]["ours"], by["Приёмки за 7 дней"]["ok"]), (1, True))   # s2 старше 7 дней
        self.assertTrue(by["Инвентаризации за 7 дней"]["ok"])

    def test_money(self):
        from datetime import datetime, timedelta
        now_ms = datetime.now(mirror.MS_TZ)
        ms = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S.000")
        acc = {"meta": {"href": "https://x/entity/organization/org1/accounts/acc1"}, "name": "Kaspi"}
        pay = lambda i, kind, s, appl=True, **kw: {"id": f"{kind}{i}", "name": str(i), "moment": ms(now_ms), "updated": ms(now_ms),
                                                    "sum": s * 100, "applicable": appl, **kw}
        self.ms.docs["paymentin"] = [pay(1, "pi", 5000, organizationAccount=acc, paymentPurpose="Оплата заказа 123"), pay(2, "pi", 99, appl=False)]
        self.ms.docs["cashin"] = [pay(1, "ci", 1500)]
        self.ms.docs["paymentout"] = [pay(1, "po", 700)]
        self.ms.money = [{"account": acc, "balance": 120000 * 100},
                         {"organization": {"meta": {"href": "https://x/entity/organization/org1"}}, "balance": 30000 * 100}]
        self.ms.balances = [{"counterparty": {"meta": {"href": "https://x/entity/counterparty/a1"}, "name": "ИП Клиент"}, "balance": -4500 * 100}]
        for t in mirror.MONEY_DOCS:
            self.assertTrue(mirror.sync_entity(t, pause=0)[1], t)
        self.assertFalse(any(c[1].get("expand") for c in self.ms.calls if c[0] in ("/entity/paymentin", "/entity/cashin")))   # у платежей нет позиций
        with mirror.db() as d:
            r = d.run("SELECT account_id, sum, descr FROM ms_doc WHERE id='pi1'", one=True)
        self.assertEqual(tuple(r), ("acc1", 5000, "Оплата заказа 123"))
        self.assertEqual(mirror.sync_money(), 3)
        with mirror.db() as d:
            self.assertEqual(sorted(tuple(x) for x in d.run("SELECT account_id, name, balance FROM ms_money", many=True)),
                             [("acc1", "Kaspi", 120000), ("cash:org1", "Касса", 30000)])
        by = {c["name"]: c for c in mirror._money_checks()}
        day = datetime.now(oh.ALMATY).strftime("%d.%m")
        self.assertEqual((by[f"Поступления {day} (платежи + ордера)"]["ours"], by[f"Поступления {day} (платежи + ордера)"]["ok"]), (6500, True))
        self.assertEqual(by[f"Выплаты {day} (платежи + ордера)"]["ours"], 700)
        self.assertTrue(by["Деньги на счетах и в кассах, всего"]["ok"])
        self.assertEqual(by["Взаиморасчёты с контрагентами, итог"]["ours"], -4500)
        self.ms.money[0]["balance"] = 125000 * 100                     # в МойСклад пришли деньги после снимка — сверка это видит
        self.assertFalse({c["name"]: c for c in mirror._money_checks()}["Деньги на счетах и в кассах, всего"]["ok"])

    def test_payment_links(self):
        from datetime import datetime, timedelta
        now_ms = datetime.now(mirror.MS_TZ)
        ms = lambda dt: dt.strftime("%Y-%m-%d %H:%M:%S.000")
        op = lambda kind, did, s: {"meta": {"href": f"https://x/api/remap/1.2/entity/{kind}/{did}"}, "linkedSum": s * 100}
        p = {"id": "pi1", "name": "1", "moment": ms(now_ms), "updated": ms(now_ms), "sum": 7000 * 100, "applicable": True,
             "operations": [op("customerorder", "o1", 5000), op("demand", "d9", 2000)]}
        self.ms.docs["paymentin"] = [p]
        mirror.sync_entity("paymentin", pause=0)
        with mirror.db() as d:
            rows = sorted(tuple(x) for x in d.run("SELECT doc_id, doc_type, sum FROM ms_payment_link WHERE payment_id='pi1'", many=True))
        self.assertEqual(rows, [("d9", "demand", 2000), ("o1", "customerorder", 5000)])
        p["operations"], p["updated"] = [op("customerorder", "o1", 7000)], ms(now_ms + timedelta(seconds=5))   # привязку изменили
        mirror.sync_entity("paymentin", pause=0)
        self.assertEqual(self.count("SELECT COUNT(*) FROM ms_payment_link WHERE payment_id='pi1'"), 1)
        self.assertEqual(self.count("SELECT sum FROM ms_payment_link WHERE payment_id='pi1'"), 7000)

    def test_paid_sum_saved(self):
        from datetime import datetime
        now = datetime.now(mirror.MS_TZ).strftime("%Y-%m-%d %H:%M:%S.000")
        self.ms.docs["demand"] = [{"id": "dp", "name": "7", "moment": now, "updated": now, "sum": 9000 * 100,
                                   "payedSum": 4000 * 100, "applicable": True, "positions": {"meta": {"size": 0}, "rows": []}}]
        mirror.sync_entity("demand", pause=0)
        self.assertEqual(self.count("SELECT paid FROM ms_doc WHERE id='dp'"), 4000)

    def test_links_reload_once(self):
        with mirror.db() as d:
            d.run("DELETE FROM setting WHERE key='mirror_links_v'")
            d.run("INSERT INTO ms_sync (entity, cursor, skip, full_from, full_done, rows) VALUES ('paymentin', '2026-10-08 10:00:00', 3, 0, 5, 0)")
        mirror._links_reload()
        self.assertEqual(self.count("SELECT full_done FROM ms_sync WHERE entity='paymentin'"), 0)
        with mirror.db() as d:
            d.run("UPDATE ms_sync SET full_done=5 WHERE entity='paymentin'")
        mirror._links_reload()
        self.assertEqual(self.count("SELECT full_done FROM ms_sync WHERE entity='paymentin'"), 5)

    def test_lines_reload_once(self):
        with mirror.db() as d:
            d.run("DELETE FROM setting WHERE key='mirror_lines_v'")
            d.run("INSERT INTO ms_sync (entity, cursor, skip, full_from, full_done, rows) VALUES ('demand', '2026-10-08 10:00:00', 3, 0, 5, 0)")
        mirror._lines_reload()
        with mirror.db() as d:
            self.assertEqual(tuple(d.run("SELECT cursor, skip, full_done FROM ms_sync WHERE entity='demand'", one=True)), ("", 0, 0))
            d.run("UPDATE ms_sync SET full_done=5 WHERE entity='demand'")
        mirror._lines_reload()                                          # второй раз — ничего не сбрасывает
        self.assertEqual(self.count("SELECT full_done FROM ms_sync WHERE entity='demand'"), 5)

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
