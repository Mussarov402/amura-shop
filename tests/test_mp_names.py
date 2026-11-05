"""Названия контрагентов площадок (владелец 05.11.2026): «Ozon — <магазин>», «WB — <магазин>», без «(маркетплейс)»;
название магазина — переменная *_NAME, иначе из кабинета площадки; старый контрагент переименовывается (тот же id)."""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import inbox  # noqa: E402
import marketplaces as mpk  # noqa: E402
import order_hook as oh  # noqa: E402


class Names(unittest.TestCase):
    def setUp(self):
        self.env = {"OZON_CLIENT_ID": "c", "OZON_API_KEY": "k", "WB_API_TOKEN": "t"}
        for k in ("OZON_NAME", "WB_NAME"):
            self.env_pop(k)
        os.environ.update(self.env)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in self.env])
        mpk._names.clear()
        mpk._renamed.clear()
        with inbox.db() as d:
            d.run("DELETE FROM setting WHERE key LIKE 'mp_%%_shopname'")
        self.alerts, self.puts = [], []
        self.patch(oh, "alert", lambda key, text, every=600: self.alerts.append(text))

    def env_pop(self, k):
        old = os.environ.pop(k, None)
        if old is not None:
            self.addCleanup(lambda: os.environ.__setitem__(k, old))

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def test_name_from_cabinet_and_format(self):
        oz, wb = mpk.Ozon(), mpk.WB()
        self.patch(oz, "call", lambda path, body: {"company": {"name": "Cosmo Market", "legal_name": "ТОО Космо"}} if path == "/v1/seller/info" else {})
        self.patch(wb, "req", lambda m, base, path, **k: {"name": "ИП Иванов", "tradeMark": "Amura Beauty"} if path == "/api/v1/seller-info" else {})
        self.assertEqual((oz.agent_name, oz.mark), ("Ozon — Cosmo Market", "Заказ с Ozon (FBS) — Cosmo Market"))
        self.assertEqual((wb.agent_name, wb.mark), ("WB — Amura Beauty", "Заказ с WB (FBS) — Amura Beauty"))
        os.environ["OZON_NAME"] = "AMURA"                                     # переменная главнее кабинета
        self.addCleanup(lambda: os.environ.pop("OZON_NAME", None))
        self.assertEqual(oz.agent_name, "Ozon — AMURA")

    def test_cabinet_down_no_name(self):
        oz = mpk.Ozon()

        def boom(path, body):
            raise mpk.ApiError(500, "нет связи")
        self.patch(oz, "call", boom)
        self.assertEqual((oz.agent_name, oz.mark), ("Ozon", "Заказ с Ozon (FBS)"))

    def test_rename_existing_agent_once(self):
        oz = mpk.Ozon()
        os.environ["OZON_NAME"] = "Cosmo Market"
        self.addCleanup(lambda: os.environ.pop("OZON_NAME", None))
        cards = {"a1": {"id": "a1", "name": "Ozon — Ozon KZ (маркетплейс)"}}

        def ms(method, path, json=None, **k):
            aid = path.rsplit("/", 1)[-1]
            if method == "PUT":
                self.puts.append((aid, json))
                cards[aid].update(json)
            return dict(cards[aid])
        self.patch(oh, "ms", ms)
        mpk.rename_agent(oz, "a1")
        mpk.rename_agent(oz, "a1")                                             # второй раз за сутки — без запросов
        self.assertEqual(self.puts, [("a1", {"name": "Ozon — Cosmo Market"})])
        self.assertEqual(self.alerts, ["🟦 Контрагент площадки в МойСклад переименован: «Ozon — Ozon KZ (маркетплейс)» → «Ozon — Cosmo Market»"])

    def test_manual_name_kept(self):
        oz = mpk.Ozon()
        os.environ["OZON_NAME"] = "Ozon KZ"                                    # в переменной старое/другое название
        self.addCleanup(lambda: os.environ.pop("OZON_NAME", None))
        cards = {"a1": {"id": "a1", "name": "Ozon — Cosmo Market"}}            # владелец/Хиро переименовал вручную
        self.patch(oh, "ms", lambda method, path, json=None, **k: self.puts.append(json) if method == "PUT" else dict(cards["a1"]))
        mpk.rename_agent(oz, "a1")
        self.assertEqual((self.puts, self.alerts), ([], []))

    def test_no_rename_without_shop_name(self):
        oz = mpk.Ozon()
        self.patch(oz, "call", lambda path, body: {})
        self.patch(oh, "ms", lambda *a, **k: self.fail("без названия магазина МойСклад не трогаем"))
        mpk.rename_agent(oz, "a1")

    def test_old_agent_found_not_duplicated(self):
        wb = mpk.WB()
        os.environ["WB_NAME"] = "Amura"
        self.addCleanup(lambda: os.environ.pop("WB_NAME", None))
        with inbox.db() as d:
            d.run("DELETE FROM setting WHERE key='mp_wb_agent'")
        cards = {"w1": {"id": "w1", "name": "Wildberries — Amura (маркетплейс)"}}
        posts = []

        def ms(method, path, json=None, params=None, **k):
            if method == "GET" and path == "/entity/counterparty":
                return {"rows": [dict(c) for c in cards.values() if params["search"].lower() in c["name"].lower()]}
            if method == "POST":
                posts.append(json)
                return {"id": "new"}
            aid = path.rsplit("/", 1)[-1]
            if method == "PUT":
                cards[aid].update(json)
            return dict(cards[aid])
        self.patch(oh, "ms", ms)
        self.assertEqual(mpk.agent(wb), "w1")
        self.assertEqual((posts, cards["w1"]["name"]), ([], "WB — Amura"))


if __name__ == "__main__":
    unittest.main()
