"""Курьер Яндекс «В течение дня»: заказы дня, координаты, расчёт без оплаты, подтверждение только по кнопке."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.makedirs(os.path.join(ROOT, "fonts"), exist_ok=True)
for _f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
    if not os.path.exists(os.path.join(ROOT, "fonts", _f)):
        import shutil
        shutil.copy(os.path.join(ROOT, _f), os.path.join(ROOT, "fonts", _f))

from flask import Flask  # noqa: E402
import admin  # noqa: E402
import courier  # noqa: E402
import delivery  # noqa: E402
import inbox  # noqa: E402
import order_hook as oh  # noqa: E402

ORDER = {"id": "o1", "name": "1700", "sum": 1500000, "moment": "2026-10-09 09:00:00", "agent": {"id": "ag1", "name": "Аружан"},
         "state": {"name": "Новый"},
         "description": f"{oh.RETAIL_MARK}\nАружан, WhatsApp +77011234567\nГород: Алматы\n"
                        "Отправка: Курьер по Алматы (Яндекс) — Абая 10, кв 5, 09.10 Обед 11:00–14:00"}


class Resp:
    def __init__(self, j, code=200):
        self._j, self.status_code, self.text = j, code, json.dumps(j)

    def json(self):
        return self._j

    def raise_for_status(self):
        pass


class Courier(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.status = "ready_for_approval"

        def post(url, params=None, json=None, **kw):
            path = url.split("/v2", 1)[1]
            self.calls.append((path, params, json))
            if path == "/delivery-methods":
                return Resp({"same_day_delivery": {"allowed": True, "available_intervals": [{"from": "2026-10-09T07:00:00+00:00", "to": "2026-10-09T11:00:00+00:00"}]}})
            if path == "/claims/create":
                return Resp({"id": "cl1", "status": "estimating", "version": 1})
            if path == "/claims/info":
                return Resp({"id": "cl1", "status": self.status, "version": 2, "pricing": {"offer": {"price": "1100.00"}}, "available_cancel_state": "free"})
            if path == "/claims/accept":
                return Resp({"id": "cl1", "status": "accepted", "version": 3})
            if path == "/claims/tracking-links":
                return Resp({"route_points": [{"type": "source", "sharing_link": "https://x/src"},
                                              {"type": "destination", "sharing_link": "https://dostavka.yandex.ru/route/abc"}]})
            if path == "/claims/cancel":
                return Resp({"id": "cl1", "status": "cancelled", "version": 3})
            raise AssertionError(path)

        def get(url, params=None, **kw):
            self.calls.append(("geo", params, None))
            return Resp([{"lon": "76.94", "lat": "43.24"}])
        self.patch(courier.requests, "post", post)
        self.patch(courier.requests, "get", get)
        self.patch(oh, "ms", lambda m, p, **kw: {"rows": [ORDER]})
        self.patch(admin, "who", lambda: {"role": "owner", "perms": [], "name": "Т"})
        self.patch(delivery, "conf", lambda: {"svc": {"yandex": {"token": "t", "on": "1"}}, "store": {"wh_addr": "Туркебаева 63", "wh_phone": "+7 701 000 00 00", "wh_coords": "43.2389, 76.8897"},
                                              "rules": {"weight": 300, "box_w": 20, "box_h": 15, "box_d": 10}})
        with courier.db() as d:
            d.run("DELETE FROM ya_claim")
        for k in [k for k in oh._cache if k.startswith(("geo:", "ya_methods:"))]:
            oh._cache.pop(k)
        app = Flask(__name__)
        app.register_blueprint(courier.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def post(self, url, body):
        return json.loads(self.c.post(url, data=json.dumps(body), content_type="application/json").data)

    def test_parse_order(self):
        o = courier.parse_order(ORDER)
        self.assertEqual((o["phone"], o["addr"], o["slot"], o["sum"]), ("77011234567", "Абая 10, кв 5", "09.10 Обед 11:00–14:00", 15000))

    def test_coords(self):
        self.assertEqual(courier.parse_coords("43.2389, 76.8897"), (76.8897, 43.2389))
        self.assertEqual(courier.parse_coords("https://yandex.kz/maps/?ll=76.9450%2C43.2380&z=17"), (76.945, 43.238))
        self.assertEqual(courier.parse_coords("https://www.google.com/maps/@43.25,76.92,17z"), (76.92, 43.25))
        self.assertIsNone(courier.parse_coords("Абая 10"))
        self.assertEqual(courier.parse_coords("https://2gis.kz/almaty/geo/9430047375017163/76.889697,43.238949"), (76.889697, 43.238949))
        self.assertEqual(courier.parse_coords("https://2gis.kz/almaty?m=76.94512%2C43.23801%2F17"), (76.94512, 43.23801))

    def test_short_share_link(self):
        """«Поделиться» в 2ГИС даёт go.2gis.com/… без координат — открываем ссылку и берём их из адреса, куда она ведёт."""
        class R:
            url, text = "https://2gis.kz/almaty/geo/70000001/76.889697,43.238949", ""
        self.patch(delivery.requests, "get", lambda *a, **k: R())
        oh._cache.pop("coords:https://go.2gis.com/abc12", None)
        self.assertEqual(delivery.resolve_coords("https://go.2gis.com/abc12"), (76.889697, 43.238949))

    def test_day_list_geocodes_and_intervals(self):
        r = json.loads(self.c.get("/admin/api/courier?date=2026-10-09").data)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["intervals"]), 1)
        o = r["orders"][0]
        self.assertEqual(o["ya"]["pt"], [76.94, 43.24])
        self.assertEqual(o["statusText"], "Ещё не рассчитан")
        geo = [c for c in self.calls if c[0] == "geo"][0][1]
        self.assertNotIn("кв", geo["q"])                                  # квартиру в поиск не отправляем
        self.assertFalse([c for c in self.calls if c[0] in ("/claims/create", "/claims/accept")])   # просто открыть — ничего не создаёт

    def test_estimate_then_accept(self):
        iv = {"from": "2026-10-09T07:00:00+00:00", "to": "2026-10-09T11:00:00+00:00"}
        r = self.post("/admin/api/courier/estimate", {"date": "2026-10-09", "nums": ["1700"], "interval": iv})
        self.assertEqual(r["result"]["1700"], "ok")
        create = [c for c in self.calls if c[0] == "/claims/create"][0]
        body = create[2]
        self.assertEqual(body["same_day_data"]["delivery_interval"], iv)
        self.assertEqual(body["route_points"][0]["address"]["coordinates"], [76.8897, 43.2389])     # склад: lon, lat
        self.assertEqual(body["route_points"][1]["contact"]["phone"], "+77011234567")
        self.assertNotIn("client_requirements", body)
        self.assertFalse([c for c in self.calls if c[0] == "/claims/accept"])                     # расчёт — без подтверждения
        r = self.post("/admin/api/courier/estimate", {"date": "2026-10-09", "nums": ["1700"], "interval": iv})
        self.assertEqual(r["result"]["1700"], "уже есть заявка")                                  # повторно не создаём
        r = self.post("/admin/api/courier/accept", {"nums": ["1700"]})
        self.assertEqual(r["result"]["1700"], "ok")
        acc = [c for c in self.calls if c[0] == "/claims/accept"][0]
        self.assertEqual((acc[1]["claim_id"], acc[2]["version"]), ("cl1", 2))
        with courier.db() as d:
            row = courier._row(d, "1700")
        self.assertEqual((row["status"], row["price"]), ("accepted", 1100.0))

    def test_accept_needs_estimate_ready(self):
        self.post("/admin/api/courier/estimate", {"date": "2026-10-09", "nums": ["1700"], "interval": {"from": "a", "to": "b"}})
        self.status = "estimating"
        r = self.post("/admin/api/courier/accept", {"nums": ["1700"]})
        self.assertNotEqual(r["result"]["1700"], "ok")
        self.assertFalse([c for c in self.calls if c[0] == "/claims/accept"])

    def test_cancel(self):
        self.post("/admin/api/courier/estimate", {"date": "2026-10-09", "nums": ["1700"], "interval": {"from": "a", "to": "b"}})
        r = self.post("/admin/api/courier/cancel", {"num": "1700"})
        self.assertTrue(r["ok"])
        self.assertFalse(r["paid"])
        self.assertEqual([c for c in self.calls if c[0] == "/claims/cancel"][0][2]["cancel_state"], "free")

    def test_no_key(self):
        self.patch(delivery, "conf", lambda: {"svc": {"yandex": {}}, "store": {}, "rules": {}})
        r = json.loads(self.c.get("/admin/api/courier?date=2026-10-09").data)
        self.assertFalse(r["ready"])
        self.assertEqual(r["intervals"], [])

    def test_client_tracking(self):
        """Клиент видит статус только своих заказов; до подтверждения — ничего; в пути — ссылка «где курьер»."""
        iv = {"from": "2026-10-09T09:00:00+00:00", "to": "2026-10-09T13:00:00+00:00"}
        self.post("/admin/api/courier/estimate", {"date": "2026-10-09", "nums": ["1700"], "interval": iv})
        self.assertEqual(courier.client_tracks("ag1"), {})                    # только оценка — клиенту не показываем
        self.post("/admin/api/courier/accept", {"nums": ["1700"]})
        self.assertEqual(courier.client_tracks("ag1")["1700"]["label"], "Ищем курьера")
        self.status = "pickuped"
        courier.refresh()
        t = courier.client_tracks("ag1")["1700"]
        self.assertEqual((t["step"], t["label"]), (2, "Курьер забрал заказ и едет к вам"))
        self.assertEqual(t["link"], "https://dostavka.yandex.ru/route/abc")
        self.assertEqual(courier.client_tracks("someone-else"), {})            # чужие заказы не видны
        self.status = "delivered"
        courier.refresh()
        t = courier.client_tracks("ag1")["1700"]
        self.assertEqual((t["step"], t["link"]), (3, ""))                      # доставлен — ссылка больше не нужна

    def test_me_track_endpoint(self):
        self.post("/admin/api/courier/estimate", {"date": "2026-10-09", "nums": ["1700"], "interval": {"from": "a", "to": "b"}})
        self.post("/admin/api/courier/accept", {"nums": ["1700"]})
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        c = app.test_client()
        self.assertEqual(c.get("/me/track").status_code, 401)                  # без входа — нельзя
        r = json.loads(c.get("/me/track", headers={"Authorization": "Bearer " + oh.make_token("ag1")}).data)
        self.assertEqual(list(r["tracks"]), ["1700"])
        r = json.loads(c.get("/me/track", headers={"Authorization": "Bearer " + oh.make_token("other")}).data)
        self.assertEqual(r["tracks"], {})

    def test_push_on_status_change(self):
        """Новый статус для клиента — уведомление ему; одинаковый текст (pickup_arrived → ready_for_pickup_confirmation) — без повтора."""
        import push
        sent = []
        self.patch(push, "notify_client", lambda ag, title, body, **kw: sent.append((ag, title, body)))
        self.post("/admin/api/courier/estimate", {"date": "2026-10-09", "nums": ["1700"], "interval": {"from": "a", "to": "b"}})
        self.post("/admin/api/courier/accept", {"nums": ["1700"]})
        self.status = "pickup_arrived"
        courier.refresh()
        self.status = "ready_for_pickup_confirmation"
        courier.refresh()
        self.status = "pickuped"
        courier.refresh()
        self.assertEqual([b for _, _, b in sent], ["Курьер приехал за заказом", "Курьер забрал заказ и едет к вам"])
        self.assertTrue(all(a == "ag1" for a, _, _ in sent))

    def test_me_push_subscribe(self):
        import push
        self.patch(push, "keys", lambda: ("priv", "PUBKEY"))
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        c = app.test_client()
        self.assertEqual(json.loads(c.get("/me/push").data)["key"], "PUBKEY")
        sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
        self.assertEqual(c.post("/me/push", json={"sub": sub}).status_code, 401)
        r = c.post("/me/push", json={"sub": sub}, headers={"Authorization": "Bearer " + oh.make_token("ag1")})
        self.assertEqual(r.status_code, 200)
        with push._cdb() as d:
            self.assertEqual(d.run("SELECT agent FROM push_client WHERE endpoint=%s", (sub["endpoint"],), one=True)[0], "ag1")
        self.assertEqual(c.post("/me/push", json={"sub": {"endpoint": "x"}}, headers={"Authorization": "Bearer " + oh.make_token("ag1")}).status_code, 400)

    def test_staff_without_orders_perm(self):
        self.patch(admin, "who", lambda: {"role": "staff", "perms": ["inbox"], "name": "С"})
        self.assertEqual(self.c.get("/admin/api/courier").status_code, 403)


if __name__ == "__main__":
    unittest.main()
