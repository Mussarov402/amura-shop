"""Выгрузка: миниатюра товара, ушедшего в ноль, остаётся (вернётся на сайт сразу с фото); фото архивных удаляются."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MS_TOKEN", "test")
import export_catalog as ec  # noqa: E402

ROW = lambda pid: {"id": pid, "name": pid, "code": pid, "quantity": 2, "updated": "2026-10-17 10:00:00", "images": {"meta": {"size": 0}},
                   "salePrices": [{"priceType": {"name": ec.PRICE_OPT}, "value": 100000}, {"priceType": {"name": ec.PRICE_RTL}, "value": 150000}]}


class ExportThumbs(unittest.TestCase):
    def test_zero_stock_keeps_photo(self):
        out = tempfile.mkdtemp()
        img = os.path.join(out, "img")
        os.makedirs(img)
        for pid in ("instock", "zero", "arch"):
            open(os.path.join(img, f"{pid}.webp"), "wb").write(b"x")
        json.dump({"_ver": ec.IMG_VER, "instock": "u", "zero": "u", "arch": "u"}, open(os.path.join(img, "index.json"), "w"))
        saved = {k: getattr(ec, k) for k in ("OUT", "IMG_DIR", "fetch_assortment", "countries", "fetch_archived")}
        self.addCleanup(lambda: [setattr(ec, k, v) for k, v in saved.items()])
        ec.OUT, ec.IMG_DIR = out, img
        ec.fetch_assortment = lambda: [ROW("instock")]            # «zero» ушёл в ноль — в выгрузку не попал
        ec.countries = lambda: {}
        ec.fetch_archived = lambda: {"arch"}
        ec.main()
        left = set(os.listdir(img))
        self.assertIn("zero.webp", left)
        self.assertIn("instock.webp", left)
        self.assertNotIn("arch.webp", left)
        idx = json.load(open(os.path.join(img, "index.json")))
        self.assertEqual(set(idx) - {"_ver"}, {"instock", "zero"})
        self.assertEqual(set(json.load(open(os.path.join(img, "seen.json")))), {"instock", "zero"})


if __name__ == "__main__":
    unittest.main()


class ExportGallery(unittest.TestCase):
    """Все фото товара (владелец 25.11.2026): первое — <id>.webp, следующие — <id>-2.webp …; повторно не качаем; лишние удаляем."""
    def setUp(self):
        self.img = tempfile.mkdtemp()
        old = (ec.IMG_DIR, ec.fetch_images, ec.fetch_image)
        self.addCleanup(lambda: (setattr(ec, "IMG_DIR", old[0]), setattr(ec, "fetch_images", old[1]), setattr(ec, "fetch_image", old[2])))
        ec.IMG_DIR, self.calls = self.img, []
        ec.fetch_images = lambda pid, n=ec.MAX_IMGS: self.calls.append(n) or [b"p%d" % k for k in range(n)]
        ec.fetch_image = lambda pid: self.calls.append(1) or b"p0"

    def test_many_photos(self):
        idx = {}
        paths, got = ec.save_images("a", "u1", 3, idx)
        self.assertEqual(paths, ["img/a.webp", "img/a-2.webp", "img/a-3.webp"])
        self.assertEqual(got, 3)
        self.assertEqual(open(os.path.join(self.img, "a-3.webp"), "rb").read(), b"p2")
        self.assertEqual(ec.save_images("a", "u1", 3, idx), (paths, 0))     # тот же updated — не качаем
        paths, _ = ec.save_images("a", "u2", 1, idx)                           # фото удалили в МойСклад
        self.assertEqual(paths, ["img/a.webp"])
        self.assertFalse(os.path.exists(os.path.join(self.img, "a-2.webp")))
        self.assertEqual(ec.save_images("b", "u", 30, {})[1], ec.MAX_IMGS)     # не больше MAX_IMGS

    def test_old_index_single_photo_not_redownloaded(self):
        open(os.path.join(self.img, "c.webp"), "wb").write(b"old")
        self.assertEqual(ec.save_images("c", "u", 1, {"c": "u"}), (["img/c.webp"], 0))
        self.assertEqual(self.calls, [])
