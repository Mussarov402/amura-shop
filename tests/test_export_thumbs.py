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
