"""Карточки товаров: фото в МойСклад через очередь (без дублей), одно видео у нас (сжатие ffmpeg), модуль и права."""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401  (общая настройка: SQLite, шрифты)
import admin  # noqa: E402
import app  # noqa: E402
import cards  # noqa: E402
import inbox  # noqa: E402
import mirror  # noqa: E402
import order_hook as oh  # noqa: E402
import requests  # noqa: E402

JPG = b"\xff\xd8\xff\xe0" + b"0" * 2000


class FakeImgMS:
    """Фото товара в МойСклад: GET список, POST добавить, DELETE удалить; fail — исключения для очередных POST.
    Пишет только вызовы потока теста: фоновые потоки сервиса (проверка МойСклад при запуске order_hook через 3 с и др.)
    в это время тоже зовут oh.ms — им пустой ответ, в calls они не попадают (из-за них test_photo_validation падал раз в несколько прогонов)."""
    def __init__(self):
        self.imgs, self.calls, self.fail, self.owner = {}, [], [], threading.get_ident()

    def __call__(self, method, path, params=None, json=None, **kw):
        if threading.get_ident() != self.owner:
            return {}
        self.calls.append((method, path))
        parts = path.strip("/").split("/")          # entity/product/<pid>/images[/<iid>]
        pid = parts[2]
        lst = self.imgs.setdefault(pid, [])
        if method == "GET":
            return {"rows": [{"meta": {"href": f"https://x/entity/product/{pid}/images/{i['id']}", "downloadHref": f"https://dl/{i['id']}"},
                              "filename": i["filename"], "miniature": {"downloadHref": f"https://dl/{i['id']}/mini"}} for i in lst]}
        if method == "POST":
            for x in json:
                lst.append({"id": f"i{len(lst) + 1}", "filename": x["filename"]})
            if self.fail:
                e = self.fail.pop(0)
                if e == "lost":
                    raise requests.ConnectionError("timeout")
                lst.pop()
                raise e
            return []
        if method == "DELETE":
            self.imgs[pid] = [i for i in lst if i["id"] != parts[4]]
            return {}
        raise AssertionError(path)


class CardsTest(unittest.TestCase):
    def setUp(self):
        oh.ORDER_SECRET = b"x"
        old_ms = oh.ms
        self.ms = oh.ms = FakeImgMS()
        self.addCleanup(setattr, oh, "ms", old_ms)          # подмена — только на время теста
        self.c = app.app.test_client()
        self.h = {"Authorization": "Bearer " + admin.make_admin_token()}
        with mirror.db() as d:
            for t in ("ms_product", "ms_store", "ms_stock"):
                d.run(f"DELETE FROM {t}")
            d.run("DELETE FROM setting WHERE key='mod_cards'")
            d.run("INSERT INTO ms_product (id, kind, code, name, buy_price, prices, barcodes, archived, deleted) VALUES"
                  " ('p1', 'product', '101', 'Крем', 1200, '{\"Оптовая цена\": 2000}', '[\"4601234567893\"]', 0, 0)")
            d.run("INSERT INTO ms_store (id, name, archived) VALUES ('sA', 'Основной', 0)")
            d.run("INSERT INTO ms_stock (product_id, store_id, stock, reserve, synced) VALUES ('p1', 'sA', 7, 1, 1)")
        with cards.db() as d:
            d.run("DELETE FROM card_photo_q")
            d.run("DELETE FROM product_video")

    def up(self, data=JPG, mime="image/jpeg"):
        return self.c.post("/admin/api/cards/p1/photos", headers=self.h,
                           data={"file": (io.BytesIO(data), "a.jpg", mime)}, content_type="multipart/form-data")

    def test_card_and_module_switch(self):
        j = self.c.get("/admin/api/cards/p1", headers=self.h).get_json()["card"]
        self.assertEqual((j["name"], j["barcodes"], j["buy"], j["stock"]), ("Крем", ["4601234567893"], 1200, [{"store": "Основной", "stock": 7, "reserve": 1}]))
        self.assertEqual(self.c.get("/admin/api/cards/nope", headers=self.h).status_code, 404)
        self.c.put("/admin/api/modules", headers=self.h, json={"id": "cards", "on": False})
        self.assertEqual(self.c.get("/admin/api/cards/p1", headers=self.h).status_code, 404)              # модуль выключен — нет
        self.assertEqual(self.c.get("/admin/api/cards/p1").status_code, 401)                               # без входа — нельзя

    def test_photo_upload_list_and_proxy(self):
        r = self.up().get_json()
        self.assertEqual(r["photo"]["status"], "sent")
        self.assertEqual(len(self.ms.imgs["p1"]), 1)
        self.assertTrue(self.ms.imgs["p1"][0]["filename"].startswith("amura-"))
        with cards.db() as d:
            self.assertEqual(len(bytes(d.run("SELECT data FROM card_photo_q", one=True)[0])), 0)          # отправленное у нас не храним
        ph = self.c.get("/admin/api/cards/p1/photos", headers=self.h).get_json()
        self.assertEqual(len(ph["photos"]), 1)
        old_get = oh.S.get
        try:
            class R:
                headers, content = {"Content-Type": "image/jpeg"}, b"IMG"
                def raise_for_status(self):
                    pass
            oh.S.get = lambda url, **k: R()
            self.assertEqual(self.c.get(ph["photos"][0]["mini"]).data, b"IMG")
            self.assertEqual(self.c.get(ph["photos"][0]["mini"].split("?")[0] + "?t=bad").status_code, 403)   # без подписи — нельзя
        finally:
            oh.S.get = old_get
        self.assertEqual(self.c.delete(f"/admin/api/cards/p1/photos/{ph['photos'][0]['id']}", headers=self.h).status_code, 200)
        self.assertEqual(self.ms.imgs["p1"], [])

    def test_photo_validation(self):
        t = threading.Thread(target=oh.ms, args=("GET", "/entity/organization"))   # как проверка МойСклад при запуске сервера
        t.start()
        t.join()
        self.assertEqual(self.up(mime="image/gif").status_code, 400)
        self.assertEqual(self.up(data=b"0" * (cards.PHOTO_MAX + 1)).status_code, 400)
        self.assertEqual(self.ms.calls, [])

    def test_photo_lost_response_no_duplicate(self):
        self.ms.fail = ["lost"]
        st = self.up().get_json()["photo"]
        self.assertEqual(st["status"], "queued")                       # не потеряно — в очереди
        self.assertEqual(len(self.ms.imgs["p1"]), 1)                   # МойСклад фото принял
        with cards.db() as d:
            d.run("UPDATE card_photo_q SET next_try=0, created=0")
        self.assertEqual(cards.retry_pending(), 1)
        self.assertEqual(cards._state(st["id"])["status"], "sent")
        self.assertEqual(len(self.ms.imgs["p1"]), 1)                   # второй копии нет

    def test_photo_rejected_waits_for_manual_retry(self):
        self.ms.fail = [RuntimeError("МойСклад 412: слишком большой файл")]
        st = self.up().get_json()["photo"]
        self.assertEqual(st["status"], "error")
        with cards.db() as d:
            d.run("UPDATE card_photo_q SET next_try=0, created=0")
        self.assertEqual(cards.retry_pending(), 0)
        self.assertEqual(self.c.post(f"/admin/api/cards/photo-q/{st['id']}/retry", headers=self.h).get_json()["photo"]["status"], "sent")
        self.assertEqual(len(self.ms.imgs["p1"]), 1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "нет ffmpeg")
    def test_video_compress_and_serve(self):
        src = os.path.join(tempfile.mkdtemp(), "v.mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25", "-t", "2",
                        "-pix_fmt", "yuv420p", src], check=True)
        with open(src, "rb") as f:
            v = cards.set_video("p1", f, "v.mp4", "Владелец", sync=True)
        self.assertEqual(v["status"], "ready")
        self.assertGreater(v["size"], 1000)
        r = self.c.get(v["url"])
        self.assertEqual((r.status_code, r.mimetype), (200, "video/mp4"))
        r = self.c.get(v["url"], headers={"Range": "bytes=0-99"})
        self.assertEqual((r.status_code, len(r.data)), (206, 100))     # перемотка работает
        mime, path = cards.video_path("p1")
        self.assertTrue(os.path.exists(path))                          # кэш на диске
        r.close()
        self.assertEqual(self.c.get("/admin/api/cards/p1", headers=self.h).get_json()["card"]["video"]["status"], "ready")
        self.c.delete("/admin/api/cards/p1/video", headers=self.h)
        self.assertFalse(os.path.exists(path))                         # удалено и с диска
        self.assertEqual(self.c.get("/media/product/p1.mp4").status_code, 404)

    def test_video_bad_file(self):
        r = self.c.post("/admin/api/cards/p1/video", headers=self.h, data={"file": (io.BytesIO(b"x"), "a.txt", "text/plain")},
                        content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        v = cards.set_video("p1", io.BytesIO(b"not a video"), "a.mp4", sync=True)
        self.assertEqual(v["status"], "error")


    def test_edit_sends_only_changes(self):
        cur = {"id": "p1", "name": "Крем", "article": "", "buyPrice": {"value": 120000, "currency": {"meta": {"href": "kzt"}}},
               "salePrices": [{"value": 200000, "priceType": {"name": "Оптовая цена", "meta": {"href": "pt1"}}},
                              {"value": 300000, "priceType": {"name": "Розничная цена", "meta": {"href": "pt2"}}}],
               "barcodes": [{"ean13": "4601234567893"}]}
        sent, fail = [], []

        def ms(method, path, params=None, json=None, **kw):
            if method == "GET" and path == "/entity/product/p1":
                return cur
            if method == "PUT" and path == "/entity/product/p1":
                if fail:
                    raise fail.pop(0)
                sent.append(json)
                return {}
            raise AssertionError(path)
        oh.ms = ms
        with mirror.db() as d:
            d.run("UPDATE ms_product SET prices=%s WHERE id='p1'", ('{"Оптовая цена": 2000, "Розничная цена": 3000}',))
        r = self.c.post("/admin/api/cards/p1/edit", headers=self.h,
                        json={"name": " Крем дневной ", "prices": {"Оптовая цена": 2100, "Розничная цена": 3000}, "buy": 1200,
                              "barcodes": ["4601234567893", "12345678", "ABC-1"]}).get_json()
        self.assertEqual(r["edit"]["status"], "sent")
        b = sent[0]
        self.assertEqual(set(b), {"name", "salePrices", "barcodes"})                               # закупка не менялась — не шлём
        self.assertEqual(b["name"], "Крем дневной")
        self.assertEqual([(p["priceType"]["name"], p["value"]) for p in b["salePrices"]], [("Оптовая цена", 210000), ("Розничная цена", 300000)])
        self.assertEqual(b["salePrices"][0]["priceType"]["meta"]["href"], "pt1")                  # вид цены сохранён
        self.assertEqual(b["barcodes"], [{"ean13": "4601234567893"}, {"ean8": "12345678"}, {"code128": "ABC-1"}])
        self.assertEqual((r["card"]["name"], r["card"]["prices"]["Оптовая цена"]), ("Крем дневной", 2100))   # сразу видно в системе
        self.assertEqual(self.c.post("/admin/api/cards/p1/edit", headers=self.h, json={"name": "Крем дневной"}).get_json()["edit"]["status"], "nothing")
        fail.append(requests.ConnectionError("timeout"))
        r = self.c.post("/admin/api/cards/p1/edit", headers=self.h, json={"buy": 1300}).get_json()["edit"]
        self.assertEqual(r["status"], "queued")                                                   # не потеряно
        self.assertEqual(self.c.post(f"/admin/api/cards/edit-q/{r['id']}/retry", headers=self.h).get_json()["edit"]["status"], "sent")
        self.assertEqual(sent[-1], {"buyPrice": {"value": 130000, "currency": {"meta": {"href": "kzt"}}}})

    def test_edit_uom_drops_same_pack(self):
        """Смена единицы «шт» → «уп»: упаковка «уп» убирается (иначе МойСклад: «[3007] Упаковка уп повторяется»), другие — как были."""
        cur = {"id": "p1", "uom": {"meta": {"href": "https://x/entity/uom/sht"}},
               "packs": [{"id": "k1", "uom": {"meta": {"href": "https://x/entity/uom/up"}}, "quantity": 1},
                         {"id": "k2", "uom": {"meta": {"href": "https://x/entity/uom/box"}}, "quantity": 20}]}
        sent = []

        def ms(method, path, params=None, json=None, **kw):
            if path == "/entity/uom":
                return {"rows": [{"id": "sht", "name": "шт"}, {"id": "up", "name": "уп"}, {"id": "box", "name": "кор"}]}
            if method == "GET" and path == "/entity/product/p1":
                return cur
            if method == "PUT" and path == "/entity/product/p1":
                sent.append(json)
                return {}
            raise AssertionError(path)
        oh.ms = ms
        cards._meta.pop("uom", None)
        with mirror.db() as d:
            d.run("UPDATE ms_product SET uom='sht' WHERE id='p1'")
            d.run("INSERT INTO ms_product (id, kind, name, archived, deleted, uom) VALUES ('p2', 'product', 'Маска', 0, 0, 'up')")
        j = self.c.get("/admin/api/cards/p1", headers=self.h).get_json()
        self.assertEqual((j["card"]["uom"], [u["name"] for u in j["uoms"]]), ("sht", ["уп", "шт"]))     # только используемые
        self.assertEqual(self.c.post("/admin/api/cards/p1/edit", headers=self.h, json={"uom": "nope"}).status_code, 400)
        r = self.c.post("/admin/api/cards/p1/edit", headers=self.h, json={"uom": "up"}).get_json()
        self.assertEqual(r["edit"]["status"], "sent")
        self.assertEqual(sent[0]["uom"]["meta"]["href"].rsplit("/", 1)[-1], "up")
        self.assertEqual([p["id"] for p in sent[0]["packs"]], ["k2"])
        self.assertEqual(r["card"]["uom"], "up")
        cur["uom"], cur["packs"] = {"meta": {"href": "https://x/entity/uom/up"}}, [cur["packs"][1]]
        self.c.post("/admin/api/cards/p1/edit", headers=self.h, json={"uom": "sht"})
        self.assertNotIn("packs", sent[1])                                                         # упаковки «шт» нет — packs не трогаем
        with mirror.db() as d:
            d.run("DELETE FROM ms_product WHERE id='p2'")

    def test_bulk_uom(self):
        """Массовая смена единицы: поиск, по каждому товару свой результат, ошибка одного не мешает остальным, только владелец."""
        packs = {"m1": [{"id": "k1", "uom": {"meta": {"href": "https://x/entity/uom/up"}}, "quantity": 1}], "m2": []}
        sent = {}

        def ms(method, path, params=None, json=None, **kw):
            if path == "/entity/uom":
                return {"rows": [{"id": "sht", "name": "шт"}, {"id": "up", "name": "уп"}]}
            pid = path.rsplit("/", 1)[-1]
            if method == "GET":
                return {"id": pid, "packs": packs.get(pid, [])}
            if method == "PUT":
                if pid == "m3":
                    raise RuntimeError("МойСклад 412: [3007] Упаковка уп повторяется")
                sent[pid] = json
                return {}
            raise AssertionError(path)
        oh.ms = ms
        cards._meta.pop("uom", None)
        with mirror.db() as d:
            for i, u in (("m1", "sht"), ("m2", "sht"), ("m3", "sht"), ("m4", "up")):
                d.run("INSERT INTO ms_product (id, kind, code, name, archived, deleted, uom) VALUES (%s, 'product', %s, %s, 0, 0, %s)",
                      (i, "c" + i, f"Ekel Маска {i}", u))
        try:
            j = self.c.get("/admin/api/cards/uom-find?q=ekel маска", headers=self.h).get_json()
            self.assertEqual((j["total"], [x["uomName"] for x in j["items"]]), (4, ["шт", "шт", "шт", "уп"]))
            self.assertEqual([u["name"] for u in j["uoms"]], ["уп", "шт"])
            self.assertEqual(self.c.get("/admin/api/cards/uom-find?q=", headers=self.h).get_json()["total"], 0)
            r = self.c.post("/admin/api/cards/uom-bulk", headers=self.h, json={"ids": ["m1", "m2", "m3", "m4", "nope"], "uom": "up"}).get_json()
            st = {x["id"]: x["status"] for x in r["results"]}
            self.assertEqual(st, {"m1": "sent", "m2": "sent", "m3": "error", "m4": "nothing", "nope": "error"})
            self.assertEqual(sent["m1"]["packs"], [])                     # упаковка «уп» убрана
            self.assertNotIn("packs", sent["m2"])
            self.assertIn("3007", next(x["error"] for x in r["results"] if x["id"] == "m3"))
            for bad in ({"ids": [], "uom": "up"}, {"ids": ["m1"], "uom": "x"}, {"ids": [f"p{i}" for i in range(101)], "uom": "up"}):
                self.assertEqual(self.c.post("/admin/api/cards/uom-bulk", headers=self.h, json=bad).status_code, 400)
            old = admin.who
            try:
                admin.who = lambda: {"role": "staff", "perms": ["products", "warehouse"], "name": "С"}
                self.assertEqual(self.c.post("/admin/api/cards/uom-bulk", json={"ids": ["m1"], "uom": "up"}).status_code, 403)
                self.assertEqual(self.c.get("/admin/api/cards/uom-find?q=ekel").status_code, 403)
            finally:
                admin.who = old
        finally:
            with mirror.db() as d:
                d.run("DELETE FROM ms_product WHERE id IN ('m1', 'm2', 'm3', 'm4')")

    def test_edit_validation_and_owner_only(self):
        for bad in ({"name": ""}, {"prices": {"Нет такой": 1}}, {"buy": -1}, {"barcodes": ["абв"]}, {"prices": {"Оптовая цена": "x"}}):
            self.assertEqual(self.c.post("/admin/api/cards/p1/edit", headers=self.h, json=bad).status_code, 400, bad)
        old = admin.who
        try:
            admin.who = lambda: {"role": "staff", "perms": ["products"], "name": "С"}
            self.assertEqual(self.c.post("/admin/api/cards/p1/edit", json={"name": "X"}).status_code, 403)   # правка — только владелец
            self.assertEqual(self.c.get("/admin/api/cards/p1").status_code, 200)                              # смотреть — можно
        finally:
            admin.who = old

    def _new_ms(self, fail=None):
        created, calls = [], []

        def ms(method, path, params=None, json=None, **kw):
            calls.append((method, path))
            if path == "/entity/productfolder":
                return {"rows": [{"id": "f1", "name": "Кремы", "pathName": "Уход"}, {"id": "f0", "name": "Старое", "archived": True}]}
            if path == "/context/companysettings/pricetype":
                return [{"id": "pt1", "name": "Оптовая цена"}, {"id": "pt2", "name": "Розничная цена"}]
            if method == "GET" and path == "/entity/product":
                code = (params or {}).get("filter", "").split("externalCode=")[-1]
                return {"rows": [c for c in created if c["externalCode"] == code]}
            if method == "POST" and path == "/entity/product":
                row = {"id": f"np{len(created) + 1}", "code": json.get("code", ""), "pathName": "Уход/Кремы", "updated": "2026-10-11 10:00:00.000", **json}
                created.append(row)
                if fail:
                    e = fail.pop(0)
                    if e == "lost":
                        raise requests.ConnectionError("timeout")
                    created.pop()
                    raise e
                return row
            raise AssertionError(path)
        cards._meta.clear()
        oh.ms = ms
        return created, calls

    def test_new_product(self):
        created, calls = self._new_ms()
        m = self.c.get("/admin/api/cards/new-meta", headers=self.h).get_json()
        self.assertEqual((m["folders"], [p["name"] for p in m["priceTypes"]], m["nextCode"]),
                         ([{"id": "f1", "name": "Уход/Кремы"}], ["Оптовая цена", "Розничная цена"], "102"))   # архивные папки не показываем
        r = self.c.post("/admin/api/cards/new", headers=self.h, json={"name": "Новый крем", "code": "102", "folder": "f1", "article": "A-1",
                        "prices": {"Оптовая цена": 2500}, "buy": 1500, "barcodes": ["4601234567893"]}).get_json()["new"]
        self.assertEqual((r["status"], r["pid"]), ("sent", "np1"))
        b = created[0]
        self.assertEqual(b["externalCode"], r["id"])
        self.assertEqual(b["productFolder"]["meta"]["href"].rsplit("/", 1)[-1], "f1")
        self.assertTrue(b["salePrices"][0]["priceType"]["meta"]["href"].endswith("/context/companysettings/pricetype/pt1"))
        self.assertEqual((b["salePrices"][0]["value"], b["buyPrice"]["value"], b["barcodes"]), (250000, 150000, [{"ean13": "4601234567893"}]))
        c = self.c.get("/admin/api/cards/np1", headers=self.h).get_json()["card"]                   # карточка открывается сразу
        self.assertEqual((c["name"], c["code"], c["prices"]["Оптовая цена"]), ("Новый крем", "102", 2500))
        self.assertEqual(self.c.post("/admin/api/cards/new", headers=self.h, json={"name": "Другой", "code": "102"}).status_code, 400)   # код занят
        self.assertEqual(self.c.post("/admin/api/cards/new", headers=self.h, json={"name": "новый КРЕМ"}).status_code, 400)          # такое название есть
        self.assertEqual(self.c.post("/admin/api/cards/new", headers=self.h, json={"name": "X", "folder": "zzz"}).status_code, 400)
        self.assertEqual(self.c.post("/admin/api/cards/new", headers=self.h, json={"name": ""}).status_code, 400)

    def test_new_product_lost_response_no_duplicate(self):
        created, calls = self._new_ms(fail=["lost"])
        r = self.c.post("/admin/api/cards/new", headers=self.h, json={"name": "Сыворотка"}).get_json()["new"]
        self.assertEqual(r["status"], "queued")
        self.assertEqual(len(created), 1)                                  # МойСклад создал
        r = self.c.post(f"/admin/api/cards/new-q/{r['id']}/retry", headers=self.h).get_json()["new"]
        self.assertEqual((r["status"], r["pid"]), ("sent", "np1"))
        self.assertEqual(len(created), 1)                                  # второй карточки нет
        self.assertEqual([c for c in calls if c == ("POST", "/entity/product")], [("POST", "/entity/product")])

    def test_new_product_owner_only(self):
        self._new_ms()
        old = admin.who
        try:
            admin.who = lambda: {"role": "staff", "perms": ["products", "warehouse"], "name": "С"}
            self.assertEqual(self.c.post("/admin/api/cards/new", json={"name": "X"}).status_code, 403)
            self.assertEqual(self.c.get("/admin/api/cards/new-meta").status_code, 403)
        finally:
            admin.who = old

if __name__ == "__main__":
    unittest.main()
