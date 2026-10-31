"""Meta Conversions API: покупка с сервера — только при META_PIXEL_ID и META_CAPI_TOKEN на Render; event_id = номер заказа; без личных данных."""
import json
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask  # noqa: E402

import meta_capi  # noqa: E402
import order_hook as oh  # noqa: E402


class Resp:
    status_code, text = 200, "{}"


class MetaCapi(unittest.TestCase):
    def setUp(self):
        self.sent = []
        for k in ("META_PIXEL_ID", "META_CAPI_TOKEN"):
            self.addCleanup(lambda k=k, v=os.environ.get(k): os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None))
            os.environ.pop(k, None)
        self.patch(meta_capi.requests, "post", lambda url, params=None, json=None, timeout=None: self.sent.append((url, params, json)) or Resp())
        self.patch(oh, "notify_bg", lambda fn: fn())
        self.patch(oh, "site_on", lambda s: True)
        self.patch(oh, "session_cid", lambda: None)
        self.patch(oh, "order_core", lambda d, key, ip, me: ({"ok": True, "number": "1801", "total": 27000, "_data": {}}, 200))
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        self.c = app.test_client()

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def order(self, **extra):
        body = {"name": "Аня", "phone": "77011112233", "address": "Абая 1", "items": [{"id": "p1", "qty": 2, "price": 13500}],
                "clientTotal": 27000, "orderKey": "k" + str(len(self.sent)) + json.dumps(extra), **extra}
        return self.c.post("/order", json=body, headers={"User-Agent": "UA-test", "X-Forwarded-For": "5.6.7.8", "Referer": "https://amura.kz/"})

    def test_off_without_render_vars(self):
        self.assertEqual(self.order().status_code, 200)
        self.assertEqual(self.sent, [])
        os.environ["META_PIXEL_ID"] = "123"                                             # одного ID мало — нужен и токен
        self.order(mode="retail")
        self.assertEqual(self.sent, [])

    def test_purchase_same_event_id_no_personal_data(self):
        os.environ.update(META_PIXEL_ID="123", META_CAPI_TOKEN="tok")
        self.assertTrue(self.order(mode="retail", fbp="fb.1.1.2").get_json()["ok"])
        url, params, body = self.sent[0]
        self.assertEqual((url, params), ("https://graph.facebook.com/v21.0/123/events", {"access_token": "tok"}))
        e = body["data"][0]
        self.assertEqual((e["event_name"], e["event_id"], e["action_source"]), ("Purchase", "1801", "website"))
        self.assertEqual(e["custom_data"], {"currency": "KZT", "value": 27000, "content_type": "product", "content_ids": ["p1"],
                                            "num_items": 2, "site": "retail"})
        self.assertEqual(e["user_data"], {"client_ip_address": "5.6.7.8", "client_user_agent": "UA-test", "fbp": "fb.1.1.2"})
        raw = json.dumps(body, ensure_ascii=False)
        for pii in ("Аня", "77011112233", "Абая"):
            self.assertNotIn(pii, raw)

    def test_meta_error_does_not_break_order(self):
        os.environ.update(META_PIXEL_ID="123", META_CAPI_TOKEN="tok")
        self.patch(meta_capi.requests, "post", lambda *a, **k: (_ for _ in ()).throw(meta_capi.requests.ConnectionError("нет")))
        self.assertEqual(self.order().get_json()["number"], "1801")


if __name__ == "__main__":
    unittest.main()
