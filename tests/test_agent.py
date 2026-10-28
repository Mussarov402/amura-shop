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
        with agent.inbox.db() as d:
            for k in agent.SET.values():
                agent.inbox.set_setting(d, k, "")
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

    # ---- правила самостоятельности ----
    def test_rules_off_by_default(self):
        self.assertEqual(agent.rules(), {"auto_reviews": False, "auto_questions": False, "auto_bids": 0})
        self.new("wb_question_answer", id="q1", text="Да, подходит")
        self.assertEqual(self.writes(), [])

    def test_auto_reviews_only_4_5(self):
        self.c.post("/admin/api/agent/rules", json={"auto_reviews": True})
        r = self.new("wb_feedback_answer", id="f1", text="Спасибо!").get_json()
        self.assertEqual((r["status"], r["auto"]), ("done", True))
        self.rating = 2
        r = self.new("wb_feedback_answer", id="f2", text="Нам жаль").get_json()
        self.assertEqual(r["status"], "new")                                           # 2★ — через «Да»
        self.assertEqual(len([1 for _, p, _ in self.calls if p == "/api/v1/feedbacks/answer"]), 1)

    def test_auto_bids_limit_and_money_always_yes(self):
        self.c.post("/admin/api/agent/rules", json={"auto_bids": 5000, "auto_questions": True})
        r = self.new("wb_bid", advert_id=5, nm_id=1, bid=150).get_json()
        self.assertEqual(r["status"], "done")                                          # лимит кампании 3000 ≤ 5000
        self.assertEqual([x for _, p, x in self.calls if p == "/api/advert/v1/bids"][0]["bids"][0]["nm_bids"][0]["bid_kopecks"], 15000)
        self.c.post("/admin/api/agent/rules", json={"auto_bids": 2000})
        self.assertEqual(self.new("wb_bid", advert_id=5, nm_id=1, bid=150).get_json()["status"], "new")
        self.assertEqual(self.new("wb_budget_deposit", advert_id=5, sum=1000).get_json()["status"], "new")   # деньги — всегда «Да»
        self.assertEqual([1 for _, p, _ in self.calls if p == "/adv/v1/budget/deposit"], [])


if __name__ == "__main__":
    unittest.main()
