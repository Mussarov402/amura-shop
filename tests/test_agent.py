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
        self.patch(agent, "ASK_EVERY", 0)                                          # в тестах сообщение «на согласование» — сразу
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
        self.margins, self.nosales = [], False
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
            if self.nosales:                                                         # новинка: продаж нет — эквайринга и приёмки нет
                return {"minPrice": 99999, "lines": [{"key": "commission", "label": "Комиссия", "pct": 20, "sum": 3000},
                                                     {"key": "cost", "label": "Закупка", "pct": None, "sum": 6000},
                                                     {"key": "acquiring", "label": "Эквайринг", "pct": None, "sum": None},
                                                     {"key": "processing", "label": "Приёмка и обработка FBS", "pct": None, "sum": None}]}
            return {"minPrice": 16500 if st["margin"] == 20 else 15000, "complete": self.full,
                    "lines": [{"key": "commission", "label": "Комиссия", "sum": 1},
                              {"key": "logistics" if not self.full else "acquiring", "label": "Логистика", "sum": 1 if self.full else None}]}
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

    def test_first_price(self):
        """Новая карточка без цены: первая цена — сразу, если не ниже маржи 10 % (калькулятор); иначе и при стоящей цене — «Да»."""
        self.price = 0.0
        r = self.price_req(14000)                                                        # минимум для маржи 15 % — 15000
        self.assertEqual(r["status"], "new")
        self.assertEqual(self.margins[-1], 15.0)
        self.assertIn("минимум 15000", self.c.get("/agent/api/requests", headers=self.h).get_json()["items"][0]["summary"])
        self.assertEqual(self.price_req(15000, discount=20)["status"], "new")          # скидка — только «Да»
        r = self.price_req(15500)
        self.assertEqual((r["status"], r["auto"]), ("done", True), self.c.get("/agent/api/requests", headers=self.h).get_json()["items"][0]["summary"])
        self.assertEqual(self.uploads(), [{"data": [{"nmID": 244631445, "price": 15500}]}])
        self.full = False
        self.assertEqual(self.price_req(15500)["status"], "new")                        # расчёт неполный — «Да»
        self.full, self.price = True, 15500.0
        self.assertEqual(self.price_req(16000)["status"], "new")                        # цена уже стоит — правило выключено, «Да»
        self.c.post("/admin/api/agent/rules", json={"stop": True})
        self.price = 0.0
        self.assertEqual(self.price_req(15500)["status"], "new")                        # «Стоп» — всё через «Да»

    def test_first_price_error_reason_in_request(self):
        """Расчёт первой цены упал — причина в заявке (раньше была только в журнале сервера), заявка ждёт «Да»."""
        self.price = 0.0

        def boom(*a, **k):
            raise KeyError("dimensions")
        self.patch(agent, "min_price_for", boom)
        r = self.price_req(85990)
        self.assertEqual(r["status"], "new")
        summary = self.c.get("/agent/api/requests", headers=self.h).get_json()["items"][0]["summary"]
        self.assertIn("первая цена: проверка не удалась — 'dimensions' — через «Да»", summary)

    def test_first_price_new_merged_card_400(self):
        """Свежая карточка после склейки: WB на фильтр цен отвечает 400 — это «цены ещё нет», правило первой цены работает."""
        self.price = 0.0
        orig = agent.mpk.WB.req

        def req(ch, method, base, path, **kw):
            if path == "/api/v2/list/goods/filter" and kw.get("params", {}).get("limit") == 1:
                raise agent.mpk.ApiError(400, "HTTP 400: Invalid request parameters")
            return orig(ch, method, base, path, **kw)
        self.patch(agent.mpk.WB, "req", req)
        self.assertEqual(agent.cur_price(agent.mpk.CHANNELS["wb"], "244631445"), (0.0, 0.0, 0.0))
        r = self.price_req(15500)
        self.assertEqual((r["status"], r["auto"]), ("done", True), self.c.get("/agent/api/requests", headers=self.h).get_json()["items"][0]["summary"])

    def test_first_price_no_sales_and_dedupe(self):
        """Продаж нет: эквайринг 2,5 % и приёмка 0 ₸ по допущению; повтор той же заявки — без новой заявки и сообщения."""
        self.price, self.nosales = 0.0, True
        sent, timers = [], []
        self.patch(agent.oh, "notify_bg", lambda fn: sent.append(1))
        self.patch(agent, "ASK_EVERY", 600)
        self.patch(agent.threading, "Timer", lambda t, fn: timers.append(fn) or type("T", (), {"start": lambda s: None, "daemon": True})())
        r = self.price_req(8000)                                                   # минимум (6000 + 150) / (1 − 0,2 − 0,025 − 0,15) = 9840
        self.assertEqual(r["status"], "new")
        rid = r["id"]
        items = self.c.get("/agent/api/requests", headers=self.h).get_json()["items"]
        self.assertIn("минимум 9840 ₸; продаж ещё нет — эквайринг 2,5 %, приёмка и обработка 150 ₸ (допущения владельца от 03.11.2026)", items[0]["summary"])
        r2 = self.price_req(8100)
        self.assertEqual((r2["id"], r2["status"]), (rid, "new"))                     # та же заявка, обновлена
        self.price_req(8200, discount=5)                                           # другая заявка — в то же сводное сообщение
        self.assertEqual((len(timers), len(sent)), (1, 0))
        timers[0]()
        self.assertEqual(len(sent), 1)                                              # одно сообщение на обе
        self.assertEqual(len([x for x in self.c.get("/agent/api/requests", headers=self.h).get_json()["items"] if x["status"] == "new"]), 1)
        r = self.price_req(9900)
        self.assertEqual((r["status"], r["auto"]), ("done", True))

    def test_recheck_waiting_first_prices(self):
        """После смены правила: ждущие «Да» первые цены, которые теперь проходят, выполняются; цена уже стоит — не трогаем."""
        self.price, self.full = 0.0, False
        self.assertEqual(self.price_req(15500)["status"], "new")                    # расчёт был неполный — ждёт «Да»
        self.full = True
        self.assertEqual(agent.recheck_first_prices(), 1)
        self.assertEqual(self.uploads(), [{"data": [{"nmID": 244631445, "price": 15500}]}])
        self.full = False
        self.price_req(15600)
        self.full, self.price = True, 15500.0
        self.assertEqual(agent.recheck_first_prices(), 0)                            # цена уже стоит

    def test_raise_to_min_margin(self):
        """«Сам подними без моего да все карточки на 15% маржи»: только повышение, до минимума + 100 ₸, без скидки; «Стоп» выключает."""
        self.price = 14000.0                                                        # минимум для маржи 15 % в моке — 15000
        r = self.price_req(15050)
        self.assertEqual((r["status"], r["auto"]), ("done", True))
        self.assertEqual(self.uploads(), [{"data": [{"nmID": 244631445, "price": 15050}]}])
        self.assertIn("📈 Подняли цены до маржи 15 % (1)", self.tg[-1])
        self.assertIn("было 14000 ₸ → стало 15050 ₸", self.tg[-1])
        self.price = 14000.0
        r = self.price_req(15200)                                                   # выше минимума больше чем на 100 ₸
        self.assertEqual(r["status"], "new")
        self.assertIn("больше чем на 100 ₸", self.c.get("/agent/api/requests", headers=self.h).get_json()["items"][0]["summary"])
        self.assertEqual(self.price_req(13000)["status"], "new")                    # снижение — «Да»
        self.assertEqual(self.price_req(15000, discount=10)["status"], "new")       # меняется скидка — «Да»
        self.c.post("/admin/api/agent/rules", json={"stop": True})
        self.assertEqual(self.price_req(15050)["status"], "new")                    # «Стоп»
        self.assertEqual(len(self.uploads()), 1)

    def test_raise_all_and_pack(self):
        """Все карточки: ниже минимума для маржи 15 % — подтяжка до …90 ₸ правилом; упаковка 500 ₸ — один раз."""
        import mp_calc
        ch = agent.shop("wb", mpk.WB)
        self.patch(mpk, "channels", lambda: [ch])
        self.patch(mpk, "mapping", lambda c: {"products": {"501": {"nm": 244631445, "offer": "00615"}}, "ref": {"501": "p1"}})
        self.patch(agent.time, "sleep", lambda s: None)
        self.price = 14000.0                                                        # минимум в моке — 15000
        self.assertEqual(agent.raise_all(), (1, 1))
        self.assertEqual(self.uploads(), [{"data": [{"nmID": 244631445, "price": 15090}]}])
        self.price = 15090.0
        self.assertEqual(agent.raise_all(), (0, 0))                                # уже не ниже минимума
        old = mpk.setting(mp_calc.SETTINGS["pack"], "")
        self.addCleanup(lambda: mpk.set_setting(mp_calc.SETTINGS["pack"], old))
        with agent.db() as d:
            agent.inbox.set_setting(d, "mp_pack_default_v1", "")
        self.assertTrue(agent.pack_default())
        self.assertEqual(mpk.setting(mp_calc.SETTINGS["pack"], ""), "500")
        mpk.set_setting(mp_calc.SETTINGS["pack"], "300")                            # владелец поменял — второй раз не трогаем
        self.assertFalse(agent.pack_default())
        self.assertEqual(mpk.setting(mp_calc.SETTINGS["pack"], ""), "300")

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
            if path == "/content/v2/object/all":
                self.seen = params
                return Resp(200, {"data": [{"subjectID": 7, "subjectName": "Кремы", "parentID": 1}]})
            if path == "/api/content/v1/brands":
                self.seen = params
                return Resp(200, {"brands": [{"id": 5, "name": "Anua"}], "next": 0, "total": 1})
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

    OLD_DOCS = {"excludeDocuments": False, "items": [{"id": "0ce08737", "type": 2, "number": "ЕАЭС N RU Д-CA.АИ33.В.05392/20",
                                                      "startDate": "2020-12-07T00:00:00Z", "endDate": "2025-12-06T00:00:00Z", "verdict": "ok"}]}

    def test_documents_kept_replaced_and_reverted(self):
        self.card["documents"] = json.loads(json.dumps(self.OLD_DOCS))
        self.upd(title="Новое")                                                          # без documents — текущие уходят как есть, с id
        self.assertEqual(self.updates[-1][0]["documents"]["items"], [{k: v for k, v in self.OLD_DOCS["items"][0].items() if k != "verdict"}])
        for bad in ([{"type": 2, "number": "X", "startDate": "09.04.2026", "endDate": "2031-04-08"}],
                    [{"type": 6, "number": "X", "startDate": "2026-04-09", "endDate": "2031-04-08"}],
                    [{"type": 2, "number": "X", "startDate": "2026-04-09", "endDate": "2025-04-08"}],
                    {"excludeDocuments": True, "items": []}, [{"type": 2, "number": "X", "startDate": "2026-04-09", "file": "x"}]):
            self.assertEqual(self.upd(documents=bad).status_code, 400, bad)
        new = [{"type": 2, "number": "ЕАЭС N RU Д-KR.РА03.В.26158/26", "startDate": "2026-04-09", "endDate": "2031-04-08"}]
        r = self.upd(documents=new).get_json()
        self.assertEqual(r["status"], "done")
        self.assertEqual(self.updates[-1][0]["documents"], {"excludeDocuments": False, "items": [
            {"type": 2, "number": "ЕАЭС N RU Д-KR.РА03.В.26158/26", "startDate": "2026-04-09T00:00:00Z", "endDate": "2031-04-08T00:00:00Z"}]})
        self.assertEqual(r["result"]["before"]["documents"]["items"][0]["id"], "0ce08737")
        self.assertEqual(self.c.post(f"/admin/api/agent/{r['id']}/revert").get_json()["status"], "done")
        self.assertEqual(self.updates[-1][0]["documents"]["items"], [{"type": 2, "number": "ЕАЭС N RU Д-CA.АИ33.В.05392/20",
                                                                      "startDate": "2020-12-07T00:00:00Z", "endDate": "2025-12-06T00:00:00Z"}])

    def test_brand_and_revert(self):
        self.card["brand"] = "Ануа"
        self.assertEqual(self.upd(brand="  ").status_code, 400)
        r = self.upd(brand="Anua").get_json()
        self.assertEqual((r["status"], r["auto"]), ("done", True))
        self.assertEqual(self.updates[-1][0]["brand"], "Anua")
        self.assertEqual(self.updates[-1][0]["vendorCode"], "00615")                       # остальное — как было
        self.assertEqual(r["result"]["before"], {"brand": "Ануа"})
        self.assertEqual(self.c.post(f"/admin/api/agent/{r['id']}/revert").get_json()["status"], "done")
        self.assertEqual(self.updates[-1][0]["brand"], "Ануа")

    def test_forbidden_fields(self):
        for bad in ({"price": 13500}, {"vendorCode": "1"}, {"sizes": []}, {"skus": ["1"]}, {"photos": []}, {"_restore": True}):
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
        j = self.c.get("/agent/api/read/wb/subjects", headers=self.h, query_string={"p": '{"name": "крем", "limit": 10, "x": 1}'}).get_json()
        self.assertEqual(j["data"]["data"][0]["subjectID"], 7)
        self.assertEqual(self.seen, {"name": "крем", "limit": 10})                      # лишний параметр отброшен
        j = self.c.get("/agent/api/read/wb/brands", headers=self.h, query_string={"p": '{"subjectId": 7}'}).get_json()
        self.assertEqual(j["data"]["brands"][0]["name"], "Anua")
        self.assertEqual(self.seen, {"subjectId": 7})



class WBParams(unittest.TestCase):
    """GET-параметры WB: булевы — строчными (isAnswered=false), ошибка WB — с её текстом."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)
        self.urls = []

        def wb(method, url, params=None, json=None, headers=None, timeout=None):
            import requests
            self.urls.append(requests.Request(method, url, params=params).prepare().url)
            if params.get("isAnswered") not in ("true", "false"):
                return Resp(400, {"data": None, "error": True, "errorText": "Некорректный параметр isAnswered", "additionalErrors": None})
            return Resp(200, {"data": {"questions": [], "countUnanswered": 1}})
        self.patch(mpk.requests, "request", wb)

    def test_bool_lowercase(self):
        j = self.c.get("/agent/api/read/wb/questions", headers=self.h, query_string={"p": '{"isAnswered": false, "take": 50, "skip": 0}'}).get_json()
        self.assertTrue(j["ok"])
        self.assertIn("isAnswered=false", self.urls[0])

    def test_error_text(self):
        j = self.c.get("/agent/api/read/wb/questions", headers=self.h, query_string={"p": '{"isAnswered": "нет", "take": 50, "skip": 0}'}).get_json()
        self.assertIn("Некорректный параметр isAnswered", j["error"])


    def test_plain_query_params(self):
        j = self.c.get("/agent/api/read/wb/questions?isAnswered=false&take=50&skip=0", headers=self.h).get_json()
        self.assertTrue(j["ok"])
        self.assertIn("isAnswered=false&take=50&skip=0", self.urls[0])



class PgNames(unittest.TestCase):
    """Тесты идут на SQLite, а сервер — на Postgres: имена колонок модуля не должны быть зарезервированными словами Postgres
    (29.10.2026 колонка «full» уронила /agent/api/requests с 500)."""
    RESERVED = set("all analyse analyze and any array as asc asymmetric authorization binary both case cast check collate collation column "
                   "concurrently constraint create cross current_catalog current_date current_role current_schema current_time "
                   "current_timestamp current_user default deferrable desc distinct do else end except false fetch for foreign freeze "
                   "from full grant group having ilike in initially inner intersect into is isnull join lateral leading left like limit "
                   "localtime localtimestamp natural not notnull null offset on only or order outer overlaps placing primary references "
                   "returning right select session_user similar some symmetric system_user table tablesample then to trailing true union "
                   "unique user using variadic verbose when where window with".split())

    def test_columns(self):
        import re
        src = open(agent.__file__, encoding="utf-8").read()
        tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\)\"\)", src, re.S)
        self.assertEqual(len(tables), 4)
        for name, body in tables:
            cols = [c.strip().split()[0] for c in re.sub(r'"\s*"', " ", body).split(",") if c.strip() and not c.strip().startswith("PRIMARY")]
            self.assertFalse([c for c in cols if c.lower() in self.RESERVED], name)
        self.assertNotIn(" full", agent.CCOLS)



class CardTrash(unittest.TestCase):
    """wb_card_trash: в корзину WB — только после «Да»; «Вернуть как было» — /content/v2/cards/recover."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)
        self.sent = []
        base = mpk.requests.request

        def wb(method, url, params=None, json=None, headers=None, timeout=None):
            path = url.split(".wildberries.ru", 1)[1]
            if path in ("/content/v2/cards/delete/trash", "/content/v2/cards/recover"):
                self.sent.append((path, json))
                return Resp(200, {"data": None, "error": False, "errorText": ""})
            return base(method, url, params=params, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "request", wb)

    def new(self, **p):
        return self.c.post("/agent/api/requests", headers=self.h, json={"kind": "wb_card_trash", "shop": "wb", "params": p})

    def test_only_with_yes_and_recover(self):
        r = self.new(nmIDs=[138042931]).get_json()
        self.assertEqual((r["status"], r["auto"]), ("new", False))                      # даже при включённых правилах
        self.assertEqual(self.sent, [])
        self.assertIn("в корзину nmID 138042931", self.tg[-1])
        self.assertEqual(self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"}).get_json()["status"], "done")
        self.assertEqual(self.sent, [("/content/v2/cards/delete/trash", {"nmIDs": [138042931]})])
        j = self.c.post(f"/admin/api/agent/{r['id']}/revert").get_json()
        self.assertEqual(j["status"], "done")
        self.assertEqual(self.sent[-1], ("/content/v2/cards/recover", {"nmIDs": [138042931]}))
        self.assertEqual(self.c.post(f"/admin/api/agent/{r['id']}/revert").status_code, 409)

    def test_validation_and_no_injection(self):
        self.assertEqual(self.new(nmIDs=["abc"]).status_code, 400)
        self.assertEqual(self.new(nmIDs=[]).status_code, 400)
        r = self.new(nmIDs=[1], _recover=True).get_json()                                # менеджер не может подменить на «вернуть»
        self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"})
        self.assertEqual(self.sent, [("/content/v2/cards/delete/trash", {"nmIDs": [1]})])



class CardMerge(unittest.TestCase):
    """wb_card_merge: склейка карточек WB (/content/v2/cards/moveNm) — по правилу «Карточки», без «Да»."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)
        self.sent, self.wberr = [], None
        base = mpk.requests.request

        def wb(method, url, params=None, json=None, headers=None, timeout=None):
            path = url.split(".wildberries.ru", 1)[1]
            if path == "/content/v2/cards/moveNm":
                self.sent.append(json)
                return Resp(200, {"data": None, "error": bool(self.wberr), "errorText": self.wberr or ""})
            return base(method, url, params=params, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "request", wb)

    def new(self, **p):
        return self.c.post("/agent/api/requests", headers=self.h, json={"kind": "wb_card_merge", "shop": "wb", "params": p})

    def test_merge_auto_by_cards_rule(self):
        r = self.new(nmIDs=[2150405060, "2150251778"], targetIMT="4446535070").get_json()
        self.assertEqual(r["status"], "done", r)
        self.assertEqual(self.sent, [{"targetIMT": 4446535070, "nmIDs": [2150405060, 2150251778]}])

    def test_unmerge_and_validation(self):
        self.assertEqual(self.new(nmIDs=[2150405060]).get_json()["status"], "done")
        self.assertEqual(self.sent[-1], {"nmIDs": [2150405060]})                         # без targetIMT — разъединить
        self.assertEqual(self.new(nmIDs=[]).status_code, 400)
        self.assertEqual(self.new(nmIDs=list(range(1, 32))).status_code, 400)            # больше 30
        self.assertEqual(self.new(nmIDs=[1], targetIMT="x").status_code, 400)

    def test_wb_error_shown(self):
        self.wberr = "Карточки разных предметов"
        r = self.new(nmIDs=[1, 2], targetIMT=5).get_json()
        self.assertEqual(r["status"], "error")
        self.assertIn("разных предметов", json.dumps(r, ensure_ascii=False))


class NoShow(unittest.TestCase):
    """mp_noshow — «не выставлять» товар: только после «Да», при любых правилах."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)
        mpk.set_setting("mp_wb_noshow", "")
        self.addCleanup(lambda: mpk.set_setting("mp_wb_noshow", ""))

    def test_only_with_yes(self):
        new = lambda **p: self.c.post("/agent/api/requests", headers=self.h, json={"kind": "mp_noshow", "shop": "wb", "params": p})
        self.assertEqual(new(id="1", hide="да").status_code, 400)
        self.assertEqual(new(id="1", stock=5).status_code, 400)
        r = new(id="244631445").get_json()
        self.assertEqual((r["status"], r["auto"]), ("new", False))
        self.assertEqual(mpk.noshow(mpk.CHANNELS["wb"]), {})
        self.assertEqual(self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"}).get_json()["status"], "done")
        self.assertIn("244631445", mpk.noshow(mpk.CHANNELS["wb"]))
        r = new(id="244631445", hide=False).get_json()
        self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"})
        self.assertEqual(mpk.noshow(mpk.CHANNELS["wb"]), {})


class WBCardCreate(unittest.TestCase):
    """wb_card_create — сразу по правилу «Карточки», без цены, фото через media/save; wb_card_barcodes — только «Да»,
    баркоды только добавляются, таблица для владельца, сброс кэша пар."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)
        self.patch(agent, "PHOTO_WAIT", 0)
        self.cards = [{"nmID": 111, "subjectID": 7, "vendorCode": "00615", "brand": "Anua", "title": "Крем Anua PDRN",
                       "sizes": [{"chrtID": 501, "techSize": "0", "skus": ["2040705169877"]}], "photos": [{"big": "https://cdn.wb/old1.webp"}]},
                      {"nmID": 222, "subjectID": 7, "vendorCode": "00700", "brand": "Anua", "title": "Тонер Anua",
                       "sizes": [{"chrtID": 601, "skus": ["8809640730016"]}]}]
        self.sent = []
        base = mpk.requests.request

        def wb(method, url, params=None, json=None, headers=None, timeout=None):
            path = url.split(".wildberries.ru", 1)[1]
            if path == "/content/v2/get/cards/list":
                return Resp(200, {"cards": self.cards})
            if path == "/content/v2/object/charcs/7":
                return Resp(200, {"data": [{"charcID": 1, "name": "Объём", "charcType": 4}]})
            if path in ("/content/v2/cards/upload", "/content/v3/media/save", "/content/v2/cards/update"):
                self.sent.append((path, json))
                if path == "/content/v2/cards/upload":
                    v = json[0]["variants"][0]
                    self.cards.append({"nmID": 333, "subjectID": json[0]["subjectID"], "vendorCode": v["vendorCode"], "title": v["title"],
                                       "sizes": [{"chrtID": 701, "skus": v["sizes"][0]["skus"]}]})
                return Resp(200, {"data": None, "error": False, "errorText": ""})
            return base(method, url, params=params, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "request", wb)
        self.patch(oh, "panel_catalog", lambda: {"m1": {"id": "m1", "code": "00615", "name": "Anua PDRN крем 60 мл", "barcode": "8809640738135"}})

    def new(self, kind, **p):
        return self.c.post("/agent/api/requests", headers=self.h, json={"kind": kind, "shop": "wb", "params": p})

    NEW = {"subjectID": 7, "vendorCode": "00800", "title": "Сыворотка Anua", "brand": "Anua", "description": "Описание",
           "dimensions": {"length": 5, "width": 5, "height": 15, "weightBrutto": 0.2}, "characteristics": [{"id": 1, "value": 30}],
           "sizes": [{"skus": ["8809640738135"]}], "photos": ["https://amura.kz/cards/00800/1.png"]}

    def test_create_auto_without_price_with_photos(self):
        r = self.new("wb_card_create", **self.NEW).get_json()
        self.assertEqual((r["status"], r["auto"]), ("done", True))
        up = self.sent[0]
        self.assertEqual(up[0], "/content/v2/cards/upload")
        self.assertNotIn("price", json.dumps(up[1]))
        self.assertEqual(up[1][0]["variants"][0]["characteristics"], [{"id": 1, "value": 30}])
        self.assertEqual(self.sent[1], ("/content/v3/media/save", {"nmId": 333, "data": ["https://amura.kz/cards/00800/1.png"]}))
        self.assertEqual(r["result"]["nmID"], 333)

    def test_create_rejects_price_and_bad_ean(self):
        for bad in ({"price": 9000}, {"sizes": [{"skus": ["8809640738135"], "price": 9000}]}, {"sizes": [{"skus": ["8809640738136"]}]},
                    {"sizes": []}, {"photos": ["http://x/1.jpg"]}, {"title": "x" * 61}):
            r = self.new("wb_card_create", **{**self.NEW, **bad})
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.sent, [])

    def test_create_waits_for_yes_when_stopped(self):
        self.c.post("/admin/api/agent/rules", json={"stop": True})
        r = self.new("wb_card_create", **self.NEW).get_json()
        self.assertEqual((r["status"], r["auto"]), ("new", False))
        self.assertEqual(self.sent, [])

    def test_media_auto_and_revert(self):
        r = self.new("wb_card_media", nmID=111, photos=["https://amura.kz/cards/00615/1.png"]).get_json()
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["result"]["before"], ["https://cdn.wb/old1.webp"])
        j = self.c.post(f"/admin/api/agent/{r['id']}/revert").get_json()
        self.assertEqual(j["status"], "done")
        self.assertEqual(self.sent[-1], ("/content/v3/media/save", {"nmId": 111, "data": ["https://cdn.wb/old1.webp"]}))

    def test_barcodes_only_with_yes_table_and_cache(self):
        oh._cache["mp_map_wb"] = (9e12, {"x": 1})
        oh._cache["mp_map_wb2"] = (9e12, {"x": 1})
        r = self.new("wb_card_barcodes", items=[{"nmID": 111, "ean": "8809640738135"}]).get_json()
        self.assertEqual((r["status"], r["auto"]), ("new", False))                      # при включённых правилах — всё равно «Да»
        self.assertEqual(self.sent, [])
        item = next(x for x in self.c.get("/admin/api/agent").get_json()["items"] if x["id"] == r["id"])
        self.assertEqual(item["params"]["_rows"], [{"nmID": 111, "chrtID": 501, "wb": "Крем Anua PDRN", "ean": "8809640738135",
                                                    "msCode": "00615", "msName": "Anua PDRN крем 60 мл"}])
        self.assertIn("8809640738135", self.tg[-1])
        self.assertEqual(self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"}).get_json()["status"], "done")
        path, body = self.sent[0]
        self.assertEqual(path, "/content/v2/cards/update")
        self.assertEqual(body[0]["sizes"][0]["skus"], ["2040705169877", "8809640738135"])  # баркод WB остался
        self.assertNotIn("mp_map_wb", oh._cache)
        self.assertNotIn("mp_map_wb2", oh._cache)

    def test_barcodes_never_removed(self):
        for bad in ({"nmID": 111, "ean": "8809640738135", "skus": []}, {"nmID": 111, "ean": "8809640738135", "remove": "2040705169877"},
                    {"nmID": 111, "ean": "123"}, {"nmID": 111, "ean": "8809640738136"}):
            self.assertEqual(self.new("wb_card_barcodes", items=[bad]).status_code, 400, bad)
        self.assertEqual(self.new("wb_card_barcodes", items=[{"nmID": 111, "ean": "8809640730016"}]).status_code, 400)  # EAN другой карточки
        self.assertEqual(self.new("wb_card_barcodes", items=[{"nmID": 999, "ean": "8809640738135"}]).status_code, 400)  # не наша карточка
        self.assertEqual(agent.add_sku([{"chrtID": 1, "skus": ["2040705169877"]}], 1, "8809640738135")[0]["skus"],
                         ["2040705169877", "8809640738135"])
        self.assertEqual(self.sent, [])


class OzonCardUpdate(unittest.TestCase):
    """ozon_card_update — характеристики Ozon (rich-контент 11254) сразу по правилу «Карточки»; прежние — в before, «Вернуть как было»."""
    patch = tm.Channels.patch
    RICH = '{"content":[{"widgetName":"raShowcase","type":"roll","blocks":[]}],"version":0.3}'

    def setUp(self):
        Agent.setUp(self)
        self.sent = []
        self.attrs = [{"id": 85, "complex_id": 0, "values": [{"dictionary_value_id": 5, "value": "Anua"}]}]

        def oz(url, json=None, headers=None, timeout=None):
            path = url[len(mpk.CHANNELS["ozon"].api()):]
            if path == "/v4/product/info/attributes":
                return Resp(200, {"result": [{"offer_id": "00615", "attributes": self.attrs}]})
            self.sent.append((path, json))
            return Resp(200, {"task_id": 4242})
        self.patch(mpk.requests, "post", oz)

    def new(self, **p):
        return self.c.post("/agent/api/requests", headers=self.h, json={"kind": "ozon_card_update", "shop": "ozon", "params": p})

    def test_rich_auto_and_revert(self):
        r = self.new(offer_id="00615", attributes=[{"id": 11254, "values": [{"value": self.RICH}]}]).get_json()
        self.assertEqual((r["status"], r["auto"], r["result"]["task_id"]), ("done", True, 4242))
        self.assertEqual(self.sent[-1], ("/v1/product/attributes/update", {"items": [{"offer_id": "00615", "attributes": [
            {"id": 11254, "complex_id": 0, "values": [{"value": self.RICH}]}]}]}))
        self.assertEqual(r["result"]["before"], {"attributes": [{"id": 11254, "complex_id": 0, "values": []}]})   # rich не было
        j = self.c.post(f"/admin/api/agent/{r['id']}/revert").get_json()
        self.assertEqual(j["status"], "done")
        self.assertEqual(self.sent[-1][1]["items"][0]["attributes"], [{"id": 11254, "complex_id": 0, "values": []}])
        r = self.new(offer_id="00615", attributes=[{"id": 85, "values": [{"dictionary_value_id": 6}]}]).get_json()
        self.assertEqual(r["result"]["before"]["attributes"], self.attrs)                    # прежнее значение — для отката

    def test_validation_and_stop(self):
        for bad in ({"offer_id": "00615", "attributes": [{"id": 11254, "values": "x"}]}, {"offer_id": "00615", "attributes": []},
                    {"offer_id": "00615", "attributes": [{"id": 1, "values": [{"value": "a"}]}], "price": 1},
                    {"offer_id": "00615", "attributes": [{"id": 1, "values": [{"value": "a"}]}, {"id": 1, "values": [{"value": "b"}]}]}):
            self.assertEqual(self.new(**bad).status_code, 400, bad)
        self.c.post("/admin/api/agent/rules", json={"stop": True})
        r = self.new(offer_id="00615", attributes=[{"id": 11254, "values": [{"value": self.RICH}]}]).get_json()
        self.assertEqual((r["status"], r["auto"]), ("new", False))
        self.assertEqual(self.sent, [])


class PriceFloor(unittest.TestCase):
    """«Мин. цена в акциях»: min_price Ozon, пол цены только через «Да», акции ниже пола — отказ, сторож акций."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)
        with agent.db() as d:
            d.run("DELETE FROM agent_floor")
        self.sent, self.oz_actions, self.oz_prods, self.wb_promos, self.wb_noms = [], [], [], [], []
        oh._cache.pop("oz_cur:ozon", None)
        self.addCleanup(lambda: oh._cache.pop("oz_cur:ozon", None))
        import mp_calc
        self.patch(mp_calc, "to_kzt", lambda amount, cur, ms_rate=None: (float(amount) * (6.0 if cur == "RUB" else 1.0), "МиГ: 1 RUB = 6 ₸"))
        self.patch(mpk, "mapping", lambda ch: {"products": {"777": {"offer": "00615", "nm": None}}} if ch.key == "ozon" else {"products": {}})
        self.patch(mpk, "goods", lambda ch: [{"id": "00615", "name": "Anua PDRN"}] if ch.key == "ozon" else [{"id": "244631445", "name": "Крем Anua"}])
        import push
        self.patch(push, "notify", lambda *a, **k: self.tg.append("PUSH " + a[0]))

        def oz(url, json=None, headers=None, timeout=None):
            path = url[len(mpk.CHANNELS["ozon"].api()):]
            if path == "/v5/product/info/prices":
                return Resp(200, {"items": [{"offer_id": "x", "price": {"price": "1490", "currency_code": "RUB"}}]})
            if path == "/v1/actions/products":
                return Resp(200, {"result": {"products": self.oz_prods if json["offset"] == 0 else [], "total": len(self.oz_prods)}})
            self.sent.append((path, json))
            return Resp(200, {"result": {"product_ids": [777], "rejected": []}})
        self.patch(mpk.requests, "post", oz)
        self.patch(mpk.requests, "get",
                   lambda url, **k: Resp(200, {"result": self.oz_actions}))
        base = mpk.requests.request

        def wb(method, url, params=None, json=None, headers=None, timeout=None):
            path = url.split(".wildberries.ru", 1)[1]
            if path == "/api/v1/calendar/promotions":
                return Resp(200, {"data": {"promotions": self.wb_promos}})
            if path == "/api/v1/calendar/promotions/nomenclatures":
                return Resp(200, {"data": {"nomenclatures": [x for x in self.wb_noms if str(x["inAction"]).lower() == params["inAction"]]}})
            if path == "/api/v1/calendar/promotions/upload":
                self.sent.append((path, json))
                return Resp(200, {"data": {"alreadyExists": False}})
            return base(method, url, params=params, json=json, headers=headers, timeout=timeout)
        self.patch(mpk.requests, "request", wb)

    def new(self, kind, shop="ozon", **p):
        return self.c.post("/agent/api/requests", headers=self.h, json={"kind": kind, "shop": shop, "params": p})

    def yes(self, rid):
        return self.c.post(f"/admin/api/agent/{rid}", json={"decision": "yes"}).get_json()

    def test_ozon_min_price_and_auto_actions(self):
        r = self.new("ozon_price", offer_id="00615", min_price=12000, auto_action_enabled="ENABLED",
                     auto_add_to_ozon_actions_list_enabled="DISABLED").get_json()
        self.assertEqual(r["status"], "new")                                               # без цены — только «Да»
        self.assertIn("мин. цена в автоакциях 12000 ₸", self.tg[-1])
        self.yes(r["id"])
        self.assertEqual(self.sent[-1], ("/v1/product/import/prices", {"prices": [{
            "offer_id": "00615", "currency_code": "RUB", "min_price": "2000", "auto_action_enabled": "ENABLED",
            "auto_add_to_ozon_actions_list_enabled": "DISABLED", "min_price_for_auto_actions_enabled": True}]}))
        self.assertEqual(self.new("ozon_price", offer_id="00615").status_code, 400)
        self.assertEqual(self.new("ozon_price", offer_id="00615", auto_action_enabled="yes").status_code, 400)
        self.assertEqual(self.new("ozon_price", offer_id="00615", min_price=-1).status_code, 400)

    def test_floor_only_with_yes_and_promotions_checked(self):
        r = self.new("price_floor", id="00615", floor=12000).get_json()
        self.assertEqual((r["status"], r["auto"]), ("new", False))
        self.assertEqual(agent.floor_of("ozon", "00615"), 0)
        self.yes(r["id"])
        self.assertEqual(agent.floor_of("ozon", "00615"), 12000)
        self.assertEqual(self.new("price_floor", id="00615", floor="много").status_code, 400)
        bad = self.new("ozon_promotion", action_id=5, products=[{"product_id": 777, "action_price": 1900}])   # 1900 RUB = 11400 ₸
        self.assertEqual(bad.status_code, 400)
        self.assertIn("ниже минимальной", bad.get_json()["error"])
        ok = self.new("ozon_promotion", action_id=5, products=[{"product_id": 777, "action_price": 2100}]).get_json()
        self.assertEqual(self.yes(ok["id"])["status"], "done")
        agent.floor_set(mpk.CHANNELS["wb"], "244631445", 9000, "владелец")                  # WB: плановая цена акции
        self.wb_noms = [{"id": 244631445, "inAction": False, "planPrice": 8500, "currencyCode": "KZT"}]
        self.assertEqual(self.new("wb_promotion", shop="wb", promotionID=10, nmIDs=[244631445]).status_code, 400)
        self.wb_noms[0]["planPrice"] = 9500
        self.assertEqual(self.new("wb_promotion", shop="wb", promotionID=10, nmIDs=[244631445]).status_code, 200)

    def test_watch(self):
        agent.floor_set(mpk.CHANNELS["ozon"], "00615", 12000, "владелец")
        agent.floor_set(mpk.CHANNELS["wb"], "244631445", 9000, "владелец")
        self.oz_actions = [{"id": 5, "title": "Распродажа", "is_participating": True}, {"id": 6, "title": "Чужая", "is_participating": False}]
        self.oz_prods = [{"id": 777, "price": 2500, "action_price": 1900}]
        self.wb_promos = [{"id": 10, "name": "Чёрная пятница", "type": "regular", "endDateTime": "2099-01-01T00:00:00Z"},
                          {"id": 11, "name": "Авто", "type": "auto", "endDateTime": "2099-01-01T00:00:00Z"}]
        self.wb_noms = [{"id": 244631445, "inAction": True, "planPrice": 8500, "currencyCode": "KZT"}]
        found, errors = agent.floor_watch()
        self.assertEqual(errors, [])
        self.assertEqual(sorted((x["shop"], x["ident"], x["price"]) for x in found), [("ozon", "00615", 11400), ("wb", "244631445", 8500)])
        reqs = agent.rows("new")
        self.assertEqual([(x["kind"], x["params"]) for x in reqs], [("ozon_promotion_exit", {"action_id": 5, "product_ids": [777]})])
        self.assertTrue(any("выйдите в кабинете WB" in (t or "") for t in self.tg))
        self.assertTrue(any((t or "").startswith("PUSH") for t in self.tg))
        agent.floor_watch()
        self.assertEqual(len(agent.rows("new")), 1)                                          # заявка на выход не дублируется
        self.assertEqual(self.yes(reqs[0]["id"])["status"], "done")
        self.assertEqual(self.sent[-1], ("/v1/actions/products/deactivate", {"action_id": 5, "product_ids": [777]}))
        j = self.c.get("/agent/api/floor", headers=self.h).get_json()
        self.assertEqual(len(j["floors"]), 2)
        self.assertEqual(len(j["watch"]["found"]), 2)


class OzonKzCardCreate(unittest.TestCase):
    """Ozon KZ (ozon2): новые карточки — сразу по правилу «Карточки», цена не ниже безубыточности по калькулятору
    (слово владельца 02.11.2026). Ozon RU — как раньше, через «Да»."""
    patch = tm.Channels.patch
    CARD = {"offer_id": "01087", "name": "Крем Anua", "description_category_id": 1, "type_id": 2, "price": 16000,
            "depth": 60, "width": 60, "height": 120, "weight": 150, "attributes": [{"id": 85, "values": [{"value": "Anua"}]}]}

    def setUp(self):
        os.environ.update(OZON_CLIENT_ID_2="cid2", OZON_API_KEY_2="key2")
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("OZON_CLIENT_ID_2", "OZON_API_KEY_2")])
        Agent.setUp(self)
        mpk.channels()
        self.sent, self.items = [], [{"offer_id": "x", "price": {"price": "9000", "currency_code": "KZT"}, "acquiring": 100,
                                      "commissions": {"sales_percent_fbs": 15, "fbs_direct_flow_trans_max_amount": 400,
                                                      "fbs_deliv_to_customer_amount": 300, "fbs_first_mile_max_amount": 200}},
                                     {"offer_id": "y", "price": {"price": "5000", "currency_code": "KZT"}, "acquiring": 50,
                                      "commissions": {"sales_percent_fbs": 12, "fbs_direct_flow_trans_max_amount": 300,
                                                      "fbs_deliv_to_customer_amount": 200, "fbs_first_mile_max_amount": 100}}]
        oh._cache.pop("oz_cur:ozon2", None)
        self.addCleanup(lambda: oh._cache.pop("oz_cur:ozon2", None))
        import mp_calc
        self.patch(oh, "panel_catalog", lambda: {"m1": {"id": "m1", "code": "01087", "barcode": "8809640730016"}})
        self.patch(mp_calc, "product", lambda pid: {"cost": 5000})
        self.patch(mp_calc, "settings", lambda: {"tax": 4.0, "ads": 10.0, "margin": 10.0})

        def oz(url, json=None, headers=None, timeout=None):
            path = url.split(".ozon.ru", 1)[1] if ".ozon.ru" in url else url
            if path == "/v5/product/info/prices":
                return Resp(200, {"items": self.items})
            self.sent.append((path, json))
            return Resp(200, {"result": {"task_id": 777}})
        self.patch(mpk.requests, "post", oz)

    def new(self, shop="ozon2", **p):
        return self.c.post("/agent/api/requests", headers=self.h, json={"kind": "ozon_card_create", "shop": shop, "params": {**self.CARD, **p}})

    def test_kz_auto_when_above_break_even(self):
        # безубыточность: (5000 + 400+300+200+100) / (1 − (15+4+10)/100) = 6000 / 0,71 ≈ 8451 ₸
        r = self.new().get_json()
        self.assertEqual((r["status"], r["auto"]), ("done", True))
        self.assertEqual(self.sent[-1][0], "/v3/product/import")
        self.assertEqual(self.sent[-1][1]["items"][0]["price"], "16000")

    def test_kz_below_break_even_rejected(self):
        r = self.new(price=8000)
        self.assertEqual(r.status_code, 400)
        self.assertIn("ниже минимальной цены для маржи 15 %: минимум 10714 ₸", r.get_json()["error"])   # (5000 + 1000) / (1 − 0,29 − 0,15)
        self.assertEqual(self.sent, [])

    def test_no_stock_found_in_whole_ms(self):
        """Товара нет в каталоге с остатком — ищем во всём МойСклад: по коду, затем по штрихкоду (Manyo/AXIS-Y, 06.11.2026)."""
        self.patch(oh, "panel_catalog", lambda: {})
        self.patch(mpk, "ms_by_codes", lambda codes: {"01087": "m1"} if "01087" in codes else {})
        self.patch(mpk, "ms_by_barcode", lambda bc: None)
        self.assertEqual(self.new().get_json()["status"], "done")
        self.patch(mpk, "ms_by_codes", lambda codes: {})
        self.patch(mpk, "ms_by_barcode", lambda bc: "m1" if bc == "8809640730016" else None)
        self.assertEqual(self.new(barcode="8809640730016").get_json()["status"], "done")

    def test_kz_unknown_cost_waits_for_yes(self):
        self.patch(oh, "panel_catalog", lambda: {})
        self.patch(mpk, "ms_by_codes", lambda codes: {})
        self.patch(mpk, "ms_by_barcode", lambda bc: None)
        r = self.new().get_json()
        self.assertEqual((r["status"], r["auto"]), ("new", False))
        self.assertIn("нет в МойСклад", self.tg[-1])
        self.assertEqual(self.sent, [])

    def test_amura_ozon_auto_like_kz_and_stop_respected(self):
        """Основной Ozon «AMURA» (ИП AMURA, KZT) — с 05.11.2026 как Ozon KZ: сразу, если цена не ниже минимума."""
        self.patch(agent, "oz_currency", lambda ch: "KZT")
        r = self.new(shop="ozon").get_json()
        self.assertEqual((r["status"], r["auto"]), ("done", True))
        self.assertEqual(self.new(shop="ozon", price=8000).status_code, 400)          # ниже минимума — отказ, как у KZ
        self.sent.clear()
        self.c.post("/admin/api/agent/rules", json={"stop": True})
        r = self.new().get_json()
        self.assertEqual((r["status"], r["auto"]), ("new", False))
        self.assertEqual([x for x in self.sent if x[0] == "/v3/product/import"], [])   # «Стоп» — ничего не создаём


class OzonCard(unittest.TestCase):
    """ozon_card_create — только «Да» (Ozon требует цену в том же запросе); ozon_pictures — по правилу «Карточки»."""
    patch = tm.Channels.patch
    CARD = {"offer_id": "00615", "name": "Anua PDRN Hyaluronic Acid 100 Moisturizing Cream, 60 мл", "description_category_id": 17028922,
            "type_id": 970895715, "price": 16500, "depth": 60, "width": 60, "height": 120, "weight": 150,
            "images": ["https://amura.kz/cards/anua-00615/g1.png"], "barcode": "8809640738135",
            "attributes": [{"id": 85, "values": [{"value": "Anua"}]}]}

    def setUp(self):
        Agent.setUp(self)
        self.sent = []

        self.cab = "RUB"                                                              # кабинет Ozon в рублях, как у нас
        oh._cache.pop("oz_cur:ozon", None)
        self.addCleanup(lambda: oh._cache.pop("oz_cur:ozon", None))
        import mp_calc
        self.patch(mp_calc, "to_kzt", lambda amount, cur, ms_rate=None: (float(amount) * (6.0 if cur == "RUB" else 1.0),
                                                                           "курс продажи МиГ 30.10.2026 12:00: 1 RUB = 6 ₸" if cur == "RUB" else ""))

        def oz(url, json=None, headers=None, timeout=None):
            path = url[len(mpk.CHANNELS["ozon"].api()):]
            if path == "/v5/product/info/prices":
                return Resp(200, {"items": [{"offer_id": "x", "price": {"price": "1490", "currency_code": self.cab}}]})
            self.sent.append((path, json))
            if path == "/v3/product/import":
                return Resp(200, {"result": {"task_id": 555}})
            if path == "/v1/product/import/prices":
                return Resp(200, {"result": [{"offer_id": "00615", "updated": True}]})
            if path == "/v1/product/pictures/import":
                return Resp(200, {"result": {"pictures": []}})
            if path == "/v1/description-category/tree":
                return Resp(200, {"result": [{"description_category_id": 1, "category_name": "Красота"}]})
            raise AssertionError(path)
        self.patch(mpk.requests, "post", oz)

    def new(self, kind, **p):
        return self.c.post("/agent/api/requests", headers=self.h, json={"kind": kind, "shop": "ozon", "params": p})

    def test_create_only_with_yes(self):
        r = self.new("ozon_card_create", **self.CARD).get_json()
        self.assertEqual((r["status"], r["auto"]), ("new", False))                      # правило «Карточки» включено, но цена — «Да»
        self.assertEqual(self.sent, [])
        self.assertIn("новая карточка 00615", self.tg[-1])
        j = self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"}).get_json()
        self.assertEqual((j["status"], j["result"]["task_id"]), ("done", 555))
        item = self.sent[0][1]["items"][0]
        self.assertEqual((item["price"], item["currency_code"], item["dimension_unit"], item["weight_unit"]), ("2750", "RUB", "mm", "g"))
        self.assertEqual((j["result"]["price_kzt"], j["result"]["price"], j["result"]["currency"]), (16500, 2750, "RUB"))   # обе суммы
        self.assertIn("16500 ₸ → 2750 RUB (курс продажи МиГ", j["result"]["note"])

    def test_tenge_cabinet_and_explicit_currency(self):
        self.cab = "KZT"
        r = self.new("ozon_card_create", **self.CARD).get_json()
        self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"})
        self.assertEqual((self.sent[-1][1]["items"][0]["price"], self.sent[-1][1]["items"][0]["currency_code"]), ("16500", "KZT"))
        self.cab = "RUB"
        oh._cache.pop("oz_cur:ozon", None)
        r = self.new("ozon_price", offer_id="00615", price=2700, currency_code="RUB").get_json()          # цена в валюте кабинета
        self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"})
        self.assertEqual(self.sent[-1][1]["prices"][0], {"offer_id": "00615", "currency_code": "RUB", "price": "2700"})
        r = self.new("ozon_price", offer_id="00615", price=16500, old_price=18000).get_json()            # в ₸ — пересчёт
        self.c.post(f"/admin/api/agent/{r['id']}", json={"decision": "yes"})
        self.assertEqual(self.sent[-1][1]["prices"][0], {"offer_id": "00615", "currency_code": "RUB", "price": "2750", "old_price": "3000"})

    def test_validation(self):
        self.assertEqual(self.new("ozon_card_create", **dict(self.CARD, images=["http://x/1.png"])).status_code, 400)
        self.assertEqual(self.new("ozon_card_create", **dict(self.CARD, evil=1)).status_code, 400)
        self.assertEqual(self.new("ozon_card_create", **dict(self.CARD, price=0)).status_code, 400)
        self.assertEqual(self.new("ozon_card_create", **{k: v for k, v in self.CARD.items() if k != "attributes"}).status_code, 400)
        self.assertEqual(self.sent, [])

    def test_pictures_by_rule(self):
        r = self.new("ozon_pictures", product_id=777, images=["https://amura.kz/cards/anua-00615/g1.png"]).get_json()
        self.assertEqual((r["status"], r["auto"]), ("done", True))
        self.assertEqual(self.sent[0], ("/v1/product/pictures/import", {"product_id": 777, "images": ["https://amura.kz/cards/anua-00615/g1.png"]}))
        self.c.post("/admin/api/agent/rules", json={"stop": True})
        self.assertEqual(self.new("ozon_pictures", product_id=777, images=["https://amura.kz/x.png"]).get_json()["status"], "new")

    def test_read_tree(self):
        j = self.c.get("/agent/api/read/ozon/category_tree", headers=self.h, query_string={"p": '{"language": "DEFAULT"}'}).get_json()
        self.assertEqual(j["data"]["result"][0]["category_name"], "Красота")


    def test_error_details(self):
        def wb(method, url, params=None, json=None, headers=None, timeout=None):
            return Resp(400, {"title": "bad request", "detail": "", "additionalErrors": {"nmIDs": "карточка в объединении"}})
        self.patch(mpk.requests, "request", wb)
        j = self.c.get("/agent/api/read/wb/questions?isAnswered=false&take=1&skip=0", headers=self.h).get_json()
        self.assertIn("bad request", j["error"])
        self.assertIn("карточка в объединении", j["error"])



class Superseded(unittest.TestCase):
    """Ждущая «Да» заявка на карточку, если ту же (вид, магазин, артикул) потом выполнили другой заявкой, — «неактуальна» (06.11.2026)."""
    def test_old_card_request_closed(self):
        with agent.db() as d:
            d.run("DELETE FROM agent_req")
            ins = lambda kind, shop, p, st: d.run("INSERT INTO agent_req (at, kind, shop, params, summary, comment, status, result, decided, who) "
                                                  "VALUES (0, %s, %s, %s, '', '', %s, '', 0, '')", (kind, shop, json.dumps(p), st), ins=True)
            old = ins("ozon_card_create", "ozon", {"offer_id": "01127", "price": 3000}, "new")
            other_shop = ins("ozon_card_create", "ozon2", {"offer_id": "01127", "price": 3000}, "new")
            other_code = ins("ozon_card_create", "ozon", {"offer_id": "00363", "price": 3000}, "new")
            ins("ozon_card_create", "ozon", {"offer_id": "01127", "price": 9000}, "done")
            later = ins("ozon_card_create", "ozon", {"offer_id": "01127", "price": 9500}, "new")      # новее выполненной — ждёт
        self.assertEqual(agent.close_superseded(), 1)
        with agent.db() as d:
            st = dict(d.run("SELECT id, status FROM agent_req", many=True))
            note = d.run("SELECT result FROM agent_req WHERE id=%s", (old,), one=True)[0]
        self.assertEqual((st[old], st[other_shop], st[other_code], st[later]), ("rejected", "new", "new", "new"))
        self.assertIn("выполнено заявкой №", note)


if __name__ == "__main__":
    unittest.main()


class OzonGuard(unittest.TestCase):
    """Ограничение для акций Ozon: min_price = цена для маржи 10 %, цену не трогаем, стоящий выше — не снижаем, «Стоп» — ничего."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)
        self.sent = []
        items = [{"offer_id": "a", "price": {"price": "9000", "old_price": "12000", "min_price": "0", "currency_code": "KZT"}},
                 {"offer_id": "b", "price": {"price": "8000", "min_price": "9999", "currency_code": "KZT"}},
                 {"offer_id": "c", "price": {"price": "7000", "currency_code": "KZT"}}]

        class Ch:
            title, key = "Ozon KZ", "ozon2"

            def call(s, path, body):
                if path == "/v5/product/info/prices":
                    return {"items": items, "cursor": ""}
                self.sent.append(body)
                return {"result": [{"offer_id": x["offer_id"], "updated": True} for x in body["prices"]]}

            def res(s, r):
                return r
        self.ch = Ch()
        self.patch(agent, "ms_match", lambda ch, ident: ({"a": "p1", "b": "p2"}.get(ident), []))
        self.patch(agent, "min_price_for", lambda ch, pid, price, margin: (7300 if margin == 10.0 else 0, "", ""))

    def test_guard(self):
        n, errs = agent.oz_guard(self.ch)
        self.assertEqual((n, errs), (1, []))
        row = self.sent[0]["prices"][0]
        self.assertEqual(row, {"offer_id": "a", "currency_code": "KZT", "price": "9000", "old_price": "12000", "min_price": "7300",
                               "min_price_for_auto_actions_enabled": True})   # b — свой минимум выше, c — нет в МойСклад
        self.c.post("/admin/api/agent/rules", json={"stop": True})
        self.assertEqual(agent.oz_guard(self.ch)[0], 0)


class AlreadySet(unittest.TestCase):
    """Цена уже стоит в кабинете: «Да» — не ошибка, а «выполнено (уже стоит)»; висящие такие заявки закрываются сами."""
    patch = tm.Channels.patch

    def setUp(self):
        PriceCorridor.setUp(self)

    def test_close_same_and_already_set(self):
        self.price = 0.0
        self.c.post("/admin/api/agent/rules", json={"stop": True})               # чтобы заявка осталась «new»
        a = PriceCorridor.price_req(self, 18000, discount=5)["id"]
        self.price = 17000.0
        self.assertEqual(agent.close_same(), 0)                                  # в кабинете другая цена — не трогаем
        PriceCorridor.price_req(self, 17000)                                     # обновила ту же заявку: теперь 17000 без скидки
        self.assertEqual(agent.close_same(), 1)
        x = {x["id"]: x for x in self.c.get("/agent/api/requests", headers=self.h).get_json()["items"]}[a]
        self.assertEqual((x["status"], x["who"]), ("done", "система (уже стоит)"))
        c = PriceCorridor.price_req(self, 16500)["id"]                           # в кабинете 17000 ≥ 16500 — неактуальна
        self.patch(agent.time, "sleep", lambda s: None)
        self.assertEqual(agent.close_same(), 1)
        x = {x["id"]: x for x in self.c.get("/agent/api/requests", headers=self.h).get_json()["items"]}[c]
        self.assertEqual((x["status"], x["who"]), ("rejected", "система (неактуальна)"))
        b = PriceCorridor.price_req(self, 18000)["id"]
        self.patch(mpk.requests, "request", lambda *a, **k: tm.Resp(400, {"errorText": "Specified prices and discounts are already set"}))
        st, res = agent.execute(b, "владелец")
        self.assertEqual(st, "done")
        self.assertIn("уже стоит", res["note"])


class DimsReport(unittest.TestCase):
    """Отчёт габаритов для коробок: товары в наличии с ШВГ и весом из МойСклад; без площадок — пустые тарифы."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)

    def test_dims(self):
        self.patch(oh, "live", lambda max_age=0: {"items": [{"id": "p1", "code": "00615", "name": "Крем", "qty": 5}]})
        self.patch(oh, "ms", lambda m, path, **k: {"rows": [{"id": "p1", "weight": 0.12, "volume": 0.1, "attributes": [
            {"name": "Ширина, см", "value": 5}, {"name": "Высота, см", "value": 12}, {"name": "Глубина, см", "value": 4}]},
            {"id": "p9", "weight": 1}]})
        self.patch(mpk, "channels", lambda: [])
        j = self.c.get("/agent/api/report/dims", headers=self.h).get_json()
        self.assertEqual(j["items"], [{"code": "00615", "name": "Крем", "qty": 5,
                                       "ms": {"weight_kg": 0.12, "volume_l": 0.1, "w_cm": 5, "h_cm": 12, "d_cm": 4}}])
        self.assertEqual(self.c.get("/agent/api/report/dims").status_code, 401)


class OzonCertificate(unittest.TestCase):
    """ozon_certificate (просьба владельца 04.11.2026): нет декларации с таким номером — создаём с файлом, есть — берём её;
    привязка к товарам по offer_id. Правило «Карточки», без «Да»."""
    patch = tm.Channels.patch

    def setUp(self):
        Agent.setUp(self)
        self.sent, self.certs, self.files = [], [], []

        def oz(url, json=None, headers=None, timeout=None, data=None, files=None):
            path = url[len(mpk.CHANNELS["ozon"].api()):]
            if path == "/v1/product/certificate/list":
                return Resp(200, {"result": {"certificates": self.certs, "page_count": 1}})
            if path == "/v1/product/certificate/create":
                self.files.append((data, files["files"][0], files["files"][2]))
                self.certs.append({"certificate_id": 42, "certificate_number": data["number"]})
                return Resp(200, {"id": 42})
            if path == "/v3/product/info/list":
                return Resp(200, {"items": [{"id": 1001, "offer_id": "00615"}, {"id": 1002, "offer_id": "00700"}]})
            self.sent.append((path, json))
            return Resp(200, {"result": True})
        self.patch(mpk.requests, "post", oz)

        class Raw:
            def read(self, n, decode_content=True):
                return b"%PDF-1.4 declaration"

        class Got:
            raw = Raw()

            def raise_for_status(self):
                pass
        self.patch(mpk.requests, "get", lambda url, **k: Got())

    def new(self, **p):
        return self.c.post("/agent/api/requests", headers=self.h, json={"kind": "ozon_certificate", "shop": "ozon", "params": p})

    D = {"number": "ЕАЭС N RU Д-KR.РА03.В.31853/25", "name": "Декларация: тонеры", "type_code": "declaration",
         "issue_date": "03.04.2025", "expire_date": "2030-04-01", "file_url": "https://amura.kz/docs/d1.pdf", "offer_ids": ["00615", "00700", "nope"]}

    def test_create_bind_once(self):
        r = self.new(**self.D).get_json()
        self.assertEqual((r["status"], r["auto"]), ("done", True))                        # правило «Карточки»
        data, fname, mime = self.files[0]
        self.assertEqual((data["issue_date"], data["expire_date"], fname, mime),
                         ("2025-04-03T00:00:00Z", "2030-04-01T00:00:00Z", "declaration.pdf", "application/pdf"))
        self.assertEqual(self.sent[-1], ("/v1/product/certificate/bind", {"certificate_id": 42, "product_id": [1001, 1002]}))
        self.assertEqual(r["result"]["not_found"], ["nope"])
        r = self.new(**dict(self.D, number="ЕАЭС № RU Д-KR.РА03.В.31853/25", file_url=None, offer_ids=["00615"])).get_json()
        self.assertEqual(len(self.files), 1)                                              # та же декларация — вторую не создаём
        self.assertFalse(r["result"]["created"])

    def test_needs_file_and_dates(self):
        r = self.new(**dict(self.D, file_url=None)).get_json()
        self.assertIn("file_url", r.get("result", {}).get("error", "") + str(r.get("error", "")) + str(r))
        self.assertEqual(self.new(**dict(self.D, issue_date="вчера")).status_code, 400)
        self.assertEqual(self.files, [])
