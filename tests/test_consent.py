"""Согласие с офертой и политикой (розница): факт и время — в комментарии заказа и у клиента при входе по SMS; опт не трогаем."""
import hashlib
import json
import os
import sys
import tempfile
import time
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
import order_hook as oh  # noqa: E402

ITEM = {"id": "p1", "name": "Крем", "qty": 50, "opt": 5000, "mid": 4900, "box": 4800, "boxQty": 40, "rtl": 6690}


class Consent(unittest.TestCase):
    def setUp(self):
        self.posted = []
        for name, val in {"catalog": lambda: {"p1": dict(ITEM)}, "too_many": lambda *a, **k: False,
                          "find_or_create_agent": lambda *a: "agent-1", "organization": lambda: "org-1", "store_id": lambda: "store-1",
                          "fee_service_id": lambda: "fee-1", "delivery_service_id": lambda: "dlv-1", "notify_bg": lambda fn: None,
                          "is_wholesale": lambda cid: True, "opt_min": lambda: 0, "mark_retail": lambda *a: None,
                          "_post_order": lambda body: self.posted.append(body) or {"name": str(1500 + len(self.posted)), "id": "o" + str(len(self.posted))}}.items():
            self.patch(oh, name, val)

    def patch(self, obj, name, val):
        old = getattr(obj, name)
        setattr(obj, name, val)
        self.addCleanup(lambda: setattr(obj, name, old))

    def order(self, **extra):
        d = {"name": "Тамирис", "phone": "+77012345678", "city": "Алматы", "shipping": "pickup", "items": [{"id": "p1", "qty": 2}]} | extra
        res, st = oh.order_core(d, "c" + str(len(self.posted)) + json.dumps(extra), "1.1.1.1", "cid-1")
        self.assertEqual(st, 200, res)
        return self.posted[-1]["description"]

    def test_order_description(self):
        self.assertIn(oh.CONSENT_TXT + ": ", self.order(mode="retail", consent=True))
        self.assertIn("(Алматы)", self.posted[-1]["description"])
        self.assertNotIn(oh.CONSENT_TXT, self.order(mode="retail"))
        self.assertNotIn(oh.CONSENT_TXT, self.order(consent=True))              # опт — без строки согласия
        self.assertNotIn(oh.CONSENT_TXT, self.order(mode="retail", consent="1"))  # только настоящая галочка

    def test_login_saves_consent(self):
        cp = {"id": "cid-77", "tags": ["сайт"], "name": "Тамирис", "phone": "+77011112233"}
        self.patch(oh, "cp_by_phone", lambda p: dict(cp))
        self.patch(oh, "profile", lambda c: {"city": "Алматы"})
        app = Flask(__name__)
        app.register_blueprint(oh.bp)
        c = app.test_client()

        def verify(**kw):
            oh._codes["77011112233"] = {"h": hashlib.sha256(b"123456" + b"77011112233").hexdigest(), "t": time.time(), "tries": 0}
            return c.post("/auth/sms/verify", json={"phone": "77011112233", "code": "123456"} | kw)
        self.assertEqual(verify().status_code, 200)
        self.assertIsNone(oh.get_consent("cid-77"))                            # без галочки (опт) — ничего не пишем
        self.assertTrue(json.loads(verify(consent=True).data)["ok"])
        v = oh.get_consent("cid-77")
        self.assertEqual(v["how"], "вход по SMS")
        self.assertIn("(Алматы)", v["at"])


if __name__ == "__main__":
    unittest.main()
