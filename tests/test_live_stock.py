"""Живой каталог: оприходованный после выгрузки товар выходит на сайт сам (без ожидания часовой выгрузки),
ушедший в ноль — пропадает; фото нового товара — с сервера (/pimg), пока нет своей миниатюры."""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402
import admin  # noqa: E402
import order_hook as oh  # noqa: E402

NEW = "11111111-1111-1111-1111-111111111111"
OLD = "22222222-2222-2222-2222-222222222222"
VAR = "33333333-3333-3333-3333-333333333333"
ARC = "44444444-4444-4444-4444-444444444444"
PR = lambda opt, rtl: [{"priceType": {"name": oh.PRICE_OPT}, "value": opt * 100}, {"priceType": {"name": oh.PRICE_RTL}, "value": rtl * 100}]
MS_ROWS = {NEW: {"id": NEW, "name": "Новинка", "code": "n1", "salePrices": PR(1000, 1500), "updated": "2026-10-01 10:00:00",
                 "images": {"meta": {"size": 1}}},
           ARC: {"id": ARC, "name": "Архивный", "code": "a1", "archived": True, "salePrices": PR(1000, 1500)}}
SITE = [{"_site": True, "id": OLD, "name": "Тоник", "opt": 900, "rtl": 1200, "img": f"img/{OLD}.webp"}]


class LiveStock(unittest.TestCase):
    def setUp(self):
        self.calls, self.stock = [], {}
        saved = {k: getattr(oh, k) for k in ("ms", "_stock", "_img_index", "_products", "_delta")}
        self.addCleanup(lambda: [setattr(oh, k, v) for k, v in saved.items()])
        old_hidden = admin.hidden_ids
        admin.hidden_ids = lambda: set()
        self.addCleanup(lambda: setattr(admin, "hidden_ids", old_hidden))

        def ms(method, path, **kw):
            self.calls.append((path, kw.get("params")))
            ids = [x[3:] for x in kw["params"]["filter"].split(";")]
            return {"rows": [MS_ROWS[i] for i in ids if i in MS_ROWS]}
        oh.ms = ms
        oh._stock = lambda: dict(self.stock)
        oh._img_index = lambda: {OLD: "x"}
        oh._products = lambda: list(SITE)
        oh._delta = lambda: []
        oh._miss.clear()
        for k in ("live", "products", "delta", "imgidx"):
            oh._cache.pop(k, None)
        self.addCleanup(oh._miss.clear)
        self.addCleanup(lambda: [oh._cache.pop(k, None) for k in ("live", "products", "delta", "imgidx")])

    def shown(self):
        oh._cache.pop("live", None)
        return {i["id"]: i for i in oh.refresh(0)["items"]}

    def test_received_after_export_appears_and_zero_disappears(self):
        self.stock = {OLD: 3}
        self.assertEqual(set(self.shown()), {OLD})
        self.stock = {OLD: 3, NEW: 5, VAR: 2, ARC: 4}                  # приёмка: нового товара нет в catalog.json
        items = self.shown()
        self.assertEqual(set(items), {OLD, NEW})                       # модификация и архивный — не показываем
        self.assertEqual((items[NEW]["qty"], items[NEW]["opt"], items[NEW]["rtl"]), (5, 1000, 1500))
        self.assertTrue(items[NEW]["img"].endswith(f"/pimg/{NEW}"))     # своей миниатюры ещё нет — фото с сервера
        self.assertEqual(items[OLD]["img"], f"img/{OLD}.webp")
        n = len(self.calls)
        self.shown()
        self.assertEqual(len(self.calls), n)                           # повторно МойСклад не спрашиваем (кэш 10 / 30 минут)
        self.stock = {OLD: 0, NEW: 5}                                  # продали всё
        items = self.shown()
        self.assertEqual(set(items), {NEW})
        self.stock = {OLD: 3, NEW: 0}
        self.assertEqual(set(self.shown()), {OLD})
        self.assertNotIn(NEW, oh._miss)                                # ушёл в ноль — из догрузки убран

    def test_batches_and_limit(self):
        many = {f"{i:08d}-0000-0000-0000-000000000000": 1 for i in range(95)}
        oh._missing(set(), many)
        self.assertEqual(len(self.calls), 3)                           # по 40 id в запросе
        self.assertTrue(all(len(c[1]["filter"].split(";")) <= oh.MISS_PACK for c in self.calls))

    def test_pimg_only_live_items(self):
        self.stock = {OLD: 3, NEW: 5}
        self.shown()
        got = []
        oh._pimg.clear()
        self.addCleanup(oh._pimg.clear)
        old = oh._pimg_fetch
        oh._pimg_fetch = lambda pid: (got.append(pid), (b"RIFFwebp", "image/webp"))[1]
        self.addCleanup(lambda: setattr(oh, "_pimg_fetch", old))
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        c = app.test_client()
        r = c.get(f"/pimg/{NEW}")
        self.assertEqual((r.status_code, r.mimetype, r.data), (200, "image/webp", b"RIFFwebp"))
        self.assertIn("max-age", r.headers["Cache-Control"])
        c.get(f"/pimg/{NEW}")
        self.assertEqual(got, [NEW])                                   # второй раз — из памяти
        self.assertEqual(c.get(f"/pimg/{ARC}").status_code, 404)       # не на сайте — не отдаём
        self.assertEqual(c.get("/pimg/..%2Fx").status_code, 404)

    def test_img_url(self):
        self.assertEqual(oh.img_url(f"img/{OLD}.webp"), f"{oh.SITE_URL}/img/{OLD}.webp")
        self.assertEqual(oh.img_url("https://x/pimg/1"), "https://x/pimg/1")
        self.assertEqual(oh.img_url(None), "")


if __name__ == "__main__":
    unittest.main()
