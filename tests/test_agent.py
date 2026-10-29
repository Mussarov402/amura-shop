"""Рабочее место менеджера: токен (compare_digest), модуль выключен — 404, частота, журнал; чтение — только из списка;
действия — только заявкой, без «Да» владельца ничего не выполняется; правила без согласования; деньги — всегда через «Да»."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask  # noqa: E402
import agent  # noqa: E402
import marketplaces as mpk  # noqa: E402
import modules  # noqa: E402
import order_hook as oh  # noqa: E402
import test_marketplaces as tm  # noqa: E402

Resp = tm.Resp
TOKEN = "secret-token-123"


class Agent(unittest.TestCase):
    patch = tm.Channels.patch

    def setUp(self):
        os.environ.update(WB_API_TOKEN="tok", OZON_CLIENT_ID="cid", OZON_API_KEY="key", MP_AGENT_TOKEN=TOKEN)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("WB_API_TOKEN", "OZON_CLIENT_ID", "OZON_API_KEY", "MP_AGENT_TOKEN")])
        mpk.channels()
        modules.update("agent", on=True)
        self.addCleanup(lambda: modules.update("agent", on=False))
        with agent.db() as d:
            d.run("DELETE FROM agent_req")
            d.run("DELETE FROM agent_log")
        with agent.inbox.db() as d:                                                   # правила — как у нового магазина
            d.run("DELETE FROM setting WHERE key LIKE 'agent_%'")
        agent._hits.clear()
        self.calls, self.tg = [], []
        self.rating = 5

        def wb(method, url, params=None, json=None, headers=None, timeout=None):
            path = url.split(".wildberries.ru", 1)[1]
            self.calls.append((method, path, json))
            if path == "/api/v1/feedbacks":
                return Resp(200, {"data": {"feedbacks": [{"id": "f1", "text": "Отлично", "productValuation": 5}]}})
            if path == "/api/v1/feedback":
                return Resp(200, {"data": {"id": params["id"], "productValuation": self.rating}})
            if path in ("/api/v1/feedbacks/answer", "/api/v1/questions", "/api/v2/upload/task", "/api/advert/v1/bids", "/adv/v1/budget/deposit"):
                return Resp(200, {"data": {"id": 9}} if path == "/api/v2/upload/task" else {})
            if path == "/api/advert/v0/daily-limits":
                return Resp(200, {"adverts": [{"advertId": 5, "enabled": True, "dailyLimit": 3000}]})
            raise AssertionError((method, path))
        self.patch(mpk.requests, "request", wb)
        self.patch(oh, "notify_bg", lambda fn: fn())
        self.patch(oh, "tg", lambda method, **k: self.tg.append(k.get("text")))
        import admin
        self.patch(admin, "who", lambda: {"role": "owner", "perms": []})
        app = Flask(__name__)
        app.register_blueprint(agent.bp)
        self.c = app.test_client()
        self.h = {"Authorization": f"Bearer {TOKEN}"}

    def writes(self):
        return [p for m, p, _ in self.calls if p in ("/api/v1/feedbacks/answer", "/api/v1/questions", "/api/v2/upload/task",
                                                       "/api/advert/v1/bids", "/adv/v1/budget/deposit") and m != "GET"]

    def new(self, kind, shop="wb", **params):
        return self.c.post("/agent/api/requests", headers=self.h, json={"kind": kind, "shop": shop, "params": params, "comment": "проверка"})

    # ---- доступ ----
    def test_token(self):
        self.assertEqual(self.c.get("/agent/api/help").status_code, 401)
        self.assertEqual(self.c.get("/agent/api/help", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.c.get("/agent/api/help", headers=self.h).status_code, 200)
        os.environ["MP_AGENT_TOKEN"] = ""
        self.assertEqual(self.c.get("/agent/api/help", headers={"Authorization": "Bearer "}).status_code, 401)   # нет токена на Render
        modules.update("agent", on=False)
        self.assertEqual(self.c.get("/agent/api/help", headers=self.h).status_code, 404)

    def test_owner_session_not_enough(self):
        self.assertEqual(self.c.get("/agent/api/read/wb/feedbacks").status_code, 401)  # вход владельца не открывает API менеджера

    def test_rate_limit_and_log(self):
        self.patch(agent, "AGENT_RATE", 3)
        codes = [self.c.get("/agent/api/help", headers=self.h).status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])
        with agent.db() as d:
            st = [r[0] for r in d.run("SELECT status FROM agent_log ORDER BY id", many=True)]
        self.assertEqual(st, [200, 200, 200, 429])
        j = self.c.get("/admin/api/agent").get_json()
        self.assertEqual(len(j["log"]), 4)
        self.assertTrue(j["token"])

    # ---- чтение ----
    def test_read_whitelist(self):
        j = self.c.get("/agent/api/read/wb/feedbacks", headers=self.h,
                       query_string={"p": json.dumps({"isAnswered": False, "take": 10, "skip": 0, "evil": 1})}).get_json()
        self.assertEqual(j["data"]["data"]["feedbacks"][0]["id"], "f1")
        self.assertEqual(self.c.get("/agent/api/read/wb/delete_everything", headers=self.h).status_code, 404)
        self.assertEqual(self.c.get("/agent/api/read/wb9/feedbacks", headers=self.h).status_code, 400)
        self.assertTrue(all(m in ("GET", "POST") for m, _, _ in self.calls))

    # ---- действия: без «Да» ничего ----
    def test_request_waits_for_yes(self):
        self.rating = 3                                                                # 1–3★ — через «Да»
        r = self.new("wb_feedback_answer", id="f1", text="Спасибо за отзыв!").get_json()
        self.assertEqual((r["status"], r["auto"]), ("new", False))
        self.assertEqual(self.writes(), [])                                            # ничего не отправлено
        self.assertIn("просит согласовать", self.tg[0])
        self.assertIn("Спасибо за отзыв!", self.tg[0])
        import admin
        self.patch(admin, "who", lambda: {"role": "manager", "perms": ["orders"]})
        self.assertEqual(self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"}).status_code, 403)
        self.assertEqual(self.writes(), [])
        self.patch(admin, "who", lambda: {"role": "owner", "perms": []})
        j = self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes", "text": "Спасибо! Ждём снова"}).get_json()
        self.assertEqual(j["status"], "done")
        self.assertEqual([x for _, p, x in self.calls if p == "/api/v1/feedbacks/answer"], [{"id": "f1", "text": "Спасибо! Ждём снова"}])
        self.assertEqual(self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"}).status_code, 409)   # второй раз — нет

    def test_no_means_nothing(self):
        r = self.new("wb_price", nmID=1, price=13500).get_json()
        self.assertEqual(self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "no"}).get_json()["status"], "rejected")
        self.assertEqual(self.writes(), [])
        st = self.c.get("/agent/api/requests", headers=self.h).get_json()["items"][0]["status"]
        self.assertEqual(st, "rejected")

    def test_validation(self):
        self.assertEqual(self.new("wb_feedback_answer", id="f1", text="х").status_code, 400)
        self.assertEqual(self.new("wb_campaign", advert_id=5, action="delete").status_code, 400)
        self.assertEqual(self.new("make_money").status_code, 400)
        self.assertEqual(self.new("ozon_review_answer", shop="wb", id="1", text="Спасибо").status_code, 400)   # не тот магазин

    # ---- правила самостоятельности (решение владельца 28.10.2026) ----
    def test_rules_defaults(self):
        self.assertEqual(agent.rules(), {"auto_reviews": True, "auto_questions": True, "auto_bids": True, "auto_cards": True, "stop": False,
                                        "budgets": {"wb": 0}})

    def test_questions_auto_by_default(self):
        r = self.new("wb_question_answer", id="q1", text="Да, подходит").get_json()
        self.assertEqual((r["status"], r["auto"]), ("done", True))
        self.assertEqual([x for _, p, x in self.calls if p == "/api/v1/questions"], [{"id": "q1", "answer": {"text": "Да, подходит"}, "state": "wbRu"}])

    def test_reviews_only_4_5(self):
        r = self.new("wb_feedback_answer", id="f1", text="Спасибо!").get_json()
        self.assertEqual((r["status"], r["auto"]), ("done", True))
        self.rating = 2
        r = self.new("wb_feedback_answer", id="f2", text="Нам жаль").get_json()
        self.assertEqual(r["status"], "new")                                           # 1–3★ — через «Да»
        self.assertEqual(len([1 for _, p, _ in self.calls if p == "/api/v1/feedbacks/answer"]), 1)

    def test_ozon_review_rating(self):
        sent = []

        def oz(url, json=None, headers=None, timeout=None):
            if url.endswith("/v1/review/info"):
                return Resp(200, {"id": json["review_id"], "rating": 5 if json["review_id"] == "r5" else 3})
            if url.endswith("/v1/review/comment/create"):
                sent.append(json)
                return Resp(200, {"comment_id": "c1"})
            raise AssertionError(url)
        self.patch(mpk.requests, "post", oz)
        self.assertEqual(self.new("ozon_review_answer", shop="ozon", id="r5", text="Спасибо!").get_json()["status"], "done")
        self.assertEqual(self.new("ozon_review_answer", shop="ozon", id="r3", text="Нам жаль").get_json()["status"], "new")
        self.assertEqual([x["review_id"] for x in sent], ["r5"])

    def test_bids_need_shop_budget(self):
        self.assertEqual(self.new("wb_bid", advert_id=5, nm_id=1, bid=150).get_json()["status"], "new")      # бюджет 0 — через «Да»
        self.c.post("/admin/api/agent/rules", json={"budgets": {"wb": 5000}})
        r = self.new("wb_bid", advert_id=5, nm_id=1, bid=150).get_json()
        self.assertEqual(r["status"], "done")                                          # лимит кампании 3000 ≤ 5000
        self.assertEqual([x for _, p, x in self.calls if p == "/api/advert/v1/bids"][0]["bids"][0]["nm_bids"][0]["bid_kopecks"], 15000)
        self.c.post("/admin/api/agent/rules", json={"budgets": {"wb": 2000}})
        self.assertEqual(self.new("wb_bid", advert_id=5, nm_id=1, bid=150).get_json()["status"], "new")
        self.assertEqual(self.c.post("/admin/api/agent/rules", json={"budgets": {"wb9": 1}}).status_code, 400)

    def test_money_prices_campaigns_always_yes(self):
        self.c.post("/admin/api/agent/rules", json={"budgets": {"wb": 999999}})
        for kind, p in (("wb_budget_deposit", {"advert_id": 5, "sum": 1000}), ("wb_price", {"nmID": 1, "price": 13500}),
                        ("wb_campaign", {"advert_id": 5, "action": "start"}), ("wb_promotion", {"promotionID": 1, "nmIDs": [1]})):
            self.assertEqual(self.new(kind, **p).get_json()["status"], "new", kind)
        self.assertEqual(self.writes(), [])

    def test_stop_everything(self):
        self.c.post("/admin/api/agent/rules", json={"stop": True, "budgets": {"wb": 5000}})
        for kind, p in (("wb_question_answer", {"id": "q1", "text": "Да, подходит"}), ("wb_feedback_answer", {"id": "f1", "text": "Спасибо!"}),
                        ("wb_bid", {"advert_id": 5, "nm_id": 1, "bid": 150})):
            self.assertEqual(self.new(kind, **p).get_json()["status"], "new", kind)
        self.assertEqual(self.writes(), [])
        self.assertTrue(self.c.get("/admin/api/agent").get_json()["rules"]["stop"])

    def test_daily_digest(self):
        self.assertEqual(agent.digest(), "")
        self.new("wb_question_answer", id="q1", text="Да, подходит")
        self.new("wb_price", nmID=1, price=13500)                                      # через «Да» — не в сводке
        text = agent.digest()
        self.assertIn("без согласования за сутки (1)", text)
        self.assertIn("ответ на вопрос WB", text)
        self.assertNotIn("цена", text)

class PriceCorridor(unittest.TestCase):
    """Цены в коридоре: правило выключено по умолчанию; внутри коридора, шаг ≤5 % и раз в сутки — сразу, остальное — «Да»;
    нижняя граница — минимальная цена калькулятора для маржи 20 % (моложе 30 дней — 15 %); выросла выше цены — заявка владельцу."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)
        with agent.db() as d:
            d.run("DELETE FROM agent_corridor")
        self.price, self.disc, self.full, self.first = 17000.0, 0, True, "2026-01-01"
        self.margins = []
        import mp_calc
        wb_base = mpk.requests.request

        def wb(method, url, params=None, json=None, headers=None, timeout=None):
            if "/api/v2/list/goods/filter" in url:
                return Resp(200, {"data": {"listGoods": [{"nmID": int(params["filterNmID"]), "discount": self.disc,
                                                          "sizes": [{"price": self.price / (1 - self.disc / 100), "discountedPrice": self.price}]}]}})
            return wb_base(method, url, params=params, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "request", wb)
        self.patch(agent, "ms_match", lambda ch, ident: ("p1", [ident]))
        self.patch(mp_calc, "facts", lambda ch: {"first": {"244631445": self.first}})
        self.patch(mp_calc, "product", lambda pid: {"name": "Anua PDRN 100", "cost": 6000})
        self.patch(mp_calc, "settings", lambda: {"tax": 4, "ads": 10, "margin": 20})

        def calc(ch, pid, prod, st, price_override=None):
            self.margins.append(st["margin"])
            return {"minPrice": 16500 if st["margin"] == 20 else 15000, "complete": self.full,
                    "lines": [{"label": "Комиссия", "sum": 1}, {"label": "Эквайринг", "sum": 1 if self.full else None}]}
        self.patch(mp_calc, "channel_calc", calc)

    def on(self, base=17000):
        self.c.post("/admin/api/agent/price-rules", json={"on": True})
        return self.c.post("/admin/api/agent/corridor", json={"shop": "wb", "ident": "244631445", "base": base}).get_json()

    def price_req(self, price, **extra):
        return self.c.post("/agent/api/requests", headers=self.h,
                           json={"kind": "wb_price", "shop": "wb", "params": {"nmID": 244631445, "price": price, **extra}}).get_json()

    def uploads(self):
        return [x for _, p, x in self.calls if p == "/api/v2/upload/task"]

    def test_off_by_default(self):
        self.assertEqual(agent.price_rules(), {"on": False, "margin": 20.0, "margin_new": 15.0})
        self.assertEqual(self.price_req(17500)["status"], "new")
        self.assertEqual(self.uploads(), [])

    def test_corridor_and_auto(self):
        row = self.on()["row"]
        self.assertEqual((row["lower"], row["upper"], row["base_note"]), (16500, 19550, "задана владельцем"))   # 17000 × 1,15
        r = self.price_req(17500)
        self.assertEqual((r["status"], r["auto"]), ("done", True))                       # шаг 2,9 %
        self.assertEqual(self.uploads(), [{"data": [{"nmID": 244631445, "price": 17500}]}])
        r = self.price_req(17400)
        self.assertEqual(r["status"], "new")                                             # меньше суток с прошлой смены
        self.assertIn("меньше суток", self.c.get("/agent/api/requests", headers=self.h).get_json()["items"][0]["summary"])

    def test_outside_corridor_or_big_step(self):
        self.on()
        for price, why in ((16000, "ниже коридора: минимум 16500"), (19800, "выше коридора"), (18000, "шаг 5.9 %")):
            self.assertEqual(self.price_req(price)["status"], "new")
            self.assertIn(why, self.c.get("/agent/api/requests", headers=self.h).get_json()["items"][0]["summary"])
        self.assertEqual(self.uploads(), [])

    def test_discount_and_incomplete(self):
        self.on()
        self.assertEqual(self.price_req(17500, discount=10)["status"], "new")            # скидка — через «Да»
        self.full = False
        self.assertEqual(self.price_req(17500)["status"], "new")
        self.assertIn("неполная", self.c.get("/agent/api/requests", headers=self.h).get_json()["items"][0]["summary"])
        self.assertEqual(self.uploads(), [])

    def test_young_product_margin(self):
        self.first = (agent.mpk.datetime.now(agent.mpk.ALMATY) - agent.mpk.timedelta(days=5)).strftime("%Y-%m-%d")
        row = self.on()["row"]
        self.assertEqual((self.margins[-1], row["lower"]), (15, 15000))
        self.first = None                                                                  # продаж нет — обычная маржа
        self.assertEqual(self.on()["row"]["margin"], 20)

    def test_stop_and_base_default(self):
        self.c.post("/admin/api/agent/price-rules", json={"on": True})
        row = self.c.post("/admin/api/agent/corridor", json={"shop": "wb", "ident": "244631445"}).get_json()["row"]
        self.assertTrue(row["base_note"].startswith("цена на момент включения"))
        self.assertEqual(row["base"], 17000)
        self.c.post("/admin/api/agent/rules", json={"stop": True})
        self.assertEqual(self.price_req(17500)["status"], "new")

    def test_recheck_raises_via_yes(self):
        self.on()
        self.price = 13500.0                                                               # ставки выросли: граница 16500 > 13500
        self.assertEqual(agent.recheck_corridors(), 1)
        self.assertEqual(agent.recheck_corridors(), 0)                                     # открытая заявка — не дублируем
        x = self.c.get("/agent/api/requests", headers=self.h).get_json()["items"][0]
        self.assertEqual((x["status"], x["params"]), ("new", {"nmID": 244631445, "price": 16500}))
        self.assertIn("Нужно поднять цену", self.tg[-1])
        self.assertEqual(self.uploads(), [])                                               # сами не поднимаем



class CardUpdate(unittest.TestCase):
    """wb_card_update: только характеристики (по справочнику), габариты, название, описание; сразу по правилу владельца;
    прежняя версия — в заявке, «Вернуть как было»; запрещённые поля — отказ; «Стоп» — через «Да»."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)
        self.card = {"nmID": 244631445, "imtID": 1, "subjectID": 7, "vendorCode": "00615", "brand": "Anua", "title": "Крем Anua",
                     "description": "Старое описание", "dimensions": {"length": 10, "width": 10, "height": 20, "weightBrutto": 0.3},
                     "characteristics": [{"id": 1, "name": "Объём", "value": 50}, {"id": 2, "name": "Назначение", "value": ["увлажнение"]}],
                     "sizes": [{"chrtID": 501, "techSize": "0", "skus": ["8809640738135"]}], "photos": [{"big": "x"}]}
        self.updates = []
        base = mpk.requests.request

        def wb(method, url, params=None, json=None, headers=None, timeout=None):
            path = url.split(".wildberries.ru", 1)[1]
            if path == "/content/v2/get/cards/list":
                return Resp(200, {"cards": [self.card]})
            if path == "/content/v2/object/charcs/7":
                return Resp(200, {"data": [{"charcID": 1, "name": "Объём", "charcType": 4, "unitName": "мл"},
                                           {"charcID": 2, "name": "Назначение", "charcType": 1, "maxCount": 2},
                                           {"charcID": 3, "name": "Тип кожи", "charcType": 1, "maxCount": 0},
                                           {"charcID": 9, "name": "Старое поле", "charcType": 0}]})
            if path == "/content/v2/cards/update":
                self.updates.append(json)
                c = json[0]                                                               # WB сохранил — карточка стала такой
                self.card = dict(self.card, **{k: c[k] for k in ("title", "description", "dimensions")},
                                 characteristics=[dict(x) for x in c["characteristics"]])
                return Resp(200, {"error": False})
            return base(method, url, params=params, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "request", wb)

    def upd(self, **p):
        return self.c.post("/agent/api/requests", headers=self.h, json={"kind": "wb_card_update", "shop": "wb", "params": {"nmID": 244631445, **p}})

    def test_forbidden_fields(self):
        for bad in ({"price": 13500}, {"brand": "X"}, {"vendorCode": "1"}, {"sizes": []}, {"skus": ["1"]}, {"photos": []}, {"_restore": True}):
            r = self.upd(title="Новое", **bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("не меняются", r.get_json()["error"])
        self.assertEqual(self.upd(title="x" * 61).status_code, 400)
        self.assertEqual(self.upd().status_code, 400)
        self.assertEqual(self.upd(dimensions={"length": -1}).status_code, 400)
        self.assertEqual(self.updates, [])

    def test_auto_update_keeps_the_rest(self):
        r = self.upd(characteristics=[{"id": 1, "value": "60"}, {"id": 3, "value": ["все типы"]}], dimensions={"height": 12},
                     title="Anua PDRN крем 60 мл").get_json()
        self.assertEqual((r["status"], r["auto"]), ("done", True))
        body = self.updates[0][0]
        self.assertEqual((body["brand"], body["vendorCode"], body["sizes"]), ("Anua", "00615", self.card["sizes"]))
        self.assertNotIn("photos", body)
        self.assertEqual(body["characteristics"], [{"id": 2, "value": ["увлажнение"]}, {"id": 1, "value": 60}, {"id": 3, "value": ["все типы"]}])
        self.assertEqual(body["dimensions"], {"length": 10, "width": 10, "height": 12, "weightBrutto": 0.3})
        self.assertEqual(r["result"]["before"]["title"], "Крем Anua")
        self.assertEqual(r["result"]["before"]["characteristics"], [{"id": 1, "value": 50}, {"id": 2, "value": ["увлажнение"]}])

    def test_checked_by_reference(self):
        for chars in ([{"id": 99, "value": "x"}], [{"id": 9, "value": "x"}], [{"id": 1, "value": "много"}], [{"id": 2, "value": ["a", "b", "c"]}]):
            r = self.upd(characteristics=chars).get_json()
            self.assertEqual(r["status"], "error", chars)
        self.assertEqual(self.updates, [])

    def test_revert(self):
        rid = self.upd(characteristics=[{"id": 3, "value": ["все типы"]}], title="Новое название", description="Новое").get_json()["id"]
        self.assertEqual(self.card["title"], "Новое название")
        j = self.c.post(f"/admin/api/agent/{rid}/revert").get_json()
        self.assertEqual(j["status"], "done")
        body = self.updates[-1][0]
        self.assertEqual((body["title"], body["description"]), ("Крем Anua", "Старое описание"))
        self.assertEqual(body["characteristics"], [{"id": 1, "value": 50}, {"id": 2, "value": ["увлажнение"]}])   # добавленная — убрана
        self.assertEqual(self.c.post(f"/admin/api/agent/{rid}/revert").status_code, 409)
        import admin
        self.patch(admin, "who", lambda: {"role": "manager", "perms": ["orders"]})
        self.assertEqual(self.c.post(f"/admin/api/agent/{j['id']}/revert").status_code, 403)

    def test_stop_and_digest(self):
        self.upd(title="Anua PDRN крем 60 мл")
        self.assertIn("карточка WB", agent.digest())
        self.c.post("/admin/api/agent/rules", json={"stop": True})
        self.assertEqual(self.upd(title="Ещё").get_json()["status"], "new")
        self.assertEqual(len(self.updates), 1)

    def test_reads(self):
        j = self.c.get("/agent/api/read/wb/charcs", headers=self.h, query_string={"p": '{"subjectId": 7}'}).get_json()
        self.assertEqual(len(j["data"]["data"]), 4)


if __name__ == "__main__":
    unittest.main()
