"""Оценка доставки: точка отправления Яндекса — из настроек, иначе как у настоящих заказов, иначе из последней заявки Яндекса."""
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import delivery_estimate as de  # noqa: E402


class Source(unittest.TestCase):
    def test_last_claim_source(self):
        claims = {"claims": [{"route_points": [{"type": "destination", "address": {"coordinates": [1, 2]}},
                                               {"type": "source", "address": {"coordinates": [76.95, 43.33]}}]}]}
        r = type("R", (), {"ok": True, "json": lambda s: claims})()
        with mock.patch.object(de.requests, "post", lambda *a, **k: r):
            self.assertEqual(de.last_claim_source("t"), (76.95, 43.33))
        bad = type("R", (), {"ok": False, "json": lambda s: {}})()
        with mock.patch.object(de.requests, "post", lambda *a, **k: bad):
            self.assertIsNone(de.last_claim_source("t"))


if __name__ == "__main__":
    unittest.main()
