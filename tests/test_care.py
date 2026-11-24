"""Подбор ухода (care.py): категории по названию, подбор только из каталога в наличии и по правилам (ретиноид — вечером,
чувствительной коже — без резких активов, бюджет, «уже есть»), эконом дешевле, «Похожие дешевле», сочетания,
ИИ-тексты пачкой с фильтром запрещённых слов, модуль выключен — 404."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_mirror as tm  # noqa: E402,F401  (общая настройка: SQLite, шрифты)
import app  # noqa: E402
import care  # noqa: E402
import inbox  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402

ITEMS = [
    ("c1", "Round Lab Dokdo Cleanser", 4590, "мягкое умывание для чувствительной кожи"),
    ("c2", "Ekel Rice Foam Cleanser", 1390, "пенка для умывания, увлажняет"),
    ("t1", "Anua Heartleaf 77% Soothing Toner", 6290, "успокаивает покраснения"),
    ("s1", "Medipeel Retinal NMN Bounce Shot Booster 30ml", 6590, "ретиналь для упругости, против морщин"),
    ("s2", "Dr.Althea Gentle Vitamin C Serum", 6590, "витамин C для сияния и ровного тона"),
    ("s3", "Torriden Dive In Serum", 4590, "гиалуроновая кислота, увлажнение для сухой кожи"),
    ("s4", "Cellio 4in1 Ampoule Cica", 2890, "центелла успокаивает"),
    ("r1", "Round Lab Soybean Panthenol Cream", 5790, "пантенол, керамиды, для сухой и чувствительной кожи"),
    ("r2", "Jigott Snail Repairing Cream 100ml", 1790, "муцин улитки, увлажняет"),
    ("p1", "Round Lab Birch Juice Moisturizing Sunscreen", 6090, "SPF50, увлажняет"),
    ("p2", "Jigott Snail UV Sun Block (SPF)", 1790, "SPF"),
    ("e1", "FarmStay Real Avocado Deep Clear Peeling Gel", 1790, "пилинг с AHA кислотами"),
    ("m1", "Gabrini Derma Make-Up Cover Тональный 103", 2390, ""),
    ("f1", "Ekel Foot Cream Rose", 1290, "крем для ног"),
    ("z1", "Out Of Stock Serum", 3000, "сыворотка"),
]


class Care(unittest.TestCase):
    def setUp(self):
        items = [{"id": i, "name": n, "brand": n.split()[0], "rtl": p, "qty": 0 if i == "z1" else 5, "img": f"img/{i}.webp", "desc": d}
                 for i, n, p, d in ITEMS]
        self.patch(oh, "live", lambda *a, **k: {"items": items})
        self.patch(inbox, "OPENAI_KEY", "")
        oh._cache["descs"] = (0, {i[0]: i[3] for i in ITEMS})
        for k in ("care_ai_all", "care_cat"):
            oh._cache.pop(k, None)
        with care.db() as d:
            d.run("DELETE FROM care_ai")
        self.patch(oh, "too_many", lambda *a, **k: False)
        self.c = app.app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(setattr, obj, name, old)

    def q(self, **a):
        return care.answers_ok({"skin": "normal", "sens": "no", "concerns": [], "age": "25", "budget": "mid", "level": "std", **a})

    def ids(self, r):
        return [s["id"] for k in ("am", "pm", "week") for s in r[k]]

    def test_categories(self):
        cat = care.catalog()
        self.assertEqual({k: cat[k]["cat"] for k in ("c1", "t1", "s1", "r1", "p1", "e1", "m1", "f1")},
                         {"c1": "cleanser", "t1": "toner", "s1": "serum", "r1": "cream", "p1": "spf", "e1": "exfol", "m1": "makeup", "f1": "other"})
        self.assertNotIn("z1", cat)                                    # нет в наличии — в подбор не попадает
        self.assertEqual(care.cat_of("Zenzia Collagen Ampoule Cream"), "cream")
        self.assertEqual(care.cat_of("Round Lab Pine Cica Deep Pore Clay Mask Cleanser"), "cleanser")
        self.assertEqual(care.cat_of("Round Lab Birch Juice Moisturizing Tone Up Sunscreen"), "spf")

    def test_routine_only_catalog_retinoid_pm(self):
        cat = care.catalog()
        r = care.routine(self.q(concerns=["aging", "pigment"], level="full", budget="high"), cat)
        self.assertTrue(set(self.ids(r)) <= set(cat))
        self.assertNotIn("s1", [s["id"] for s in r["am"]])            # ретиноид — только вечером
        self.assertIn("s1", [s["id"] for s in r["pm"]])
        self.assertIn("s2", [s["id"] for s in r["am"]])               # витамин C — утром
        self.assertEqual(r["am"][-1]["step"], "spf")                  # SPF — последний шаг утра
        self.assertIn("e1", [s["id"] for s in r["week"]])
        self.assertTrue(all(cat[s["id"]]["price"] == s["price"] for k in ("am", "pm", "week") for s in r[k]))   # цены — из каталога
        self.assertIn("Ретиноид и кислоты — в разные вечера", " ".join(care.notes(r)))

    def test_sensitive_and_have(self):
        r = care.routine(self.q(sens="yes", skin="dry", concerns=["dry", "red"], have=["cleanser"]), care.catalog())
        self.assertFalse({"s1", "s2"} & set(self.ids(r)))              # чувствительной — без ретиноида и витамина C
        cl = [s for s in r["am"] if s["step"] == "cleanser"][0]
        self.assertTrue(cl["have"])
        self.assertNotIn(cl["price"], [r["total"]])

    def test_eco_cheaper_and_min_steps(self):
        cat = care.catalog()
        q = self.q(concerns=["dry"], budget="high")
        main, eco = care.routine(q, cat), care.routine(q, cat, eco=True)
        self.assertLess(eco["total"], main["total"])
        self.assertEqual({s["step"] for s in eco["am"]}, {"cleanser", "cream", "spf"})
        self.assertNotIn("f1", self.ids(eco))                          # крем для ног — не уход за лицом

    def test_similar_cheaper_and_pairing(self):
        cat = care.catalog()
        sim = care.similar("r1", cat)
        self.assertIn("r2", sim)
        self.assertTrue(all(cat[i]["price"] < cat["r1"]["price"] and cat[i]["cat"] == "cream" for i in sim))
        self.assertEqual(care.similar("m1", cat), [])
        ok, no = care.pairing(["retinoid"], "serum")
        self.assertTrue(any("кислотами" in x for x in no))
        self.assertTrue(any("солнцезащитный" in x for x in ok))

    def test_ai_batch_and_banned_words(self):
        self.patch(inbox, "OPENAI_KEY", "k")
        sent = []

        def ask(items):
            sent.append([i["id"] for i in items])
            return [{"id": i["id"], "simple": "Крем вылечит акне навсегда" if i["id"] == "r2" else f"Простое описание {i['id']}",
                     "actives": [{"name": "Пантенол", "what": "успокаивает"}], "suits": "сухой коже", "caution": "", "cat": "cream",
                     "skin": ["dry"], "concerns": ["dry", "bogus"]} for i in items]
        self.patch(care, "_ask", ask)
        self.patch(care.time, "sleep", lambda s: None)
        n = care.prepare()
        self.assertEqual(n, 14)
        self.assertEqual(len(sent), 3)                                 # пачками по 6
        a = care.ai_all()
        self.assertEqual(a["r1"]["simple"], "Простое описание r1")
        self.assertEqual(a["r2"]["simple"], "")                        # запрещённые слова — текст не показываем
        self.assertEqual(a["r1"]["concerns"], ["dry"])
        self.assertEqual(care.prepare(), 0)                            # ничего не изменилось — повторно не спрашиваем

    def test_routes_off_and_on(self):
        modules.update("care", on=False)
        self.assertEqual(self.c.get("/care/status").get_json(), {"ok": True, "on": False})
        self.assertEqual(self.c.post("/care/quiz", json={}).status_code, 404)
        self.assertEqual(self.c.get("/care/info/r1").status_code, 404)
        modules.update("care", on=True)
        self.addCleanup(lambda: modules.update("care", on=False))
        j = self.c.post("/care/quiz", json={"skin": "dry", "sens": "no", "concerns": ["dry"], "age": "25", "budget": "mid", "level": "std",
                                            "name": "Айгерим", "phone": "+77011234567"}).get_json()
        self.assertTrue(j["ok"])
        self.assertIn("не медицинская консультация", j["disclaimer"])
        self.assertTrue(j["main"]["am"] and j["eco"]["am"])
        self.assertNotIn("Айгерим", json.dumps(j, ensure_ascii=False))
        info = self.c.get("/care/info/s1").get_json()
        self.assertTrue(info["found"])
        self.assertTrue(info["avoid"])
        self.assertIn("дерматологу", info["disclaimer"])
        self.assertEqual(self.c.get("/care/info/nope").get_json()["found"], False)


if __name__ == "__main__":
    unittest.main()
