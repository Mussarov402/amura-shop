"""Ключ заказа: повтор того же заказа — тот же ответ; тот же ключ, но другая корзина/доставка — новый заказ
(иначе после сбоя связи клиент оплатил бы старый заказ: корзина 1 550 ₸ — на оплате 800 ₸ за прежний №1661)."""
import os
import sys
import tempfile
import unittest

os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("INBOX_SQLITE", os.path.join(tempfile.mkdtemp(), "t.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import order_hook as oh  # noqa: E402

BASE = {"orderKey": "abc123", "shipping": "pickup", "city": "Алматы", "address": "", "items": [{"id": "p1", "qty": 1}], "clientTotal": 800}


class OrderKey(unittest.TestCase):
    def test_same_order_same_key(self):
        self.assertEqual(oh._order_key(dict(BASE)), oh._order_key(dict(BASE, items=[{"id": "p1", "qty": 1, "price": 800}])))

    def test_changed_order_new_key(self):
        k = oh._order_key(dict(BASE))
        self.assertNotEqual(k, oh._order_key(dict(BASE, shipping="cdek", city="Шымкент", pvz={"code": "SHY1"}, clientTotal=1550)))
        self.assertNotEqual(k, oh._order_key(dict(BASE, items=[{"id": "p1", "qty": 2}])))
        self.assertNotEqual(k, oh._order_key(dict(BASE, clientTotal=1550)))
        self.assertTrue(oh._order_key({"phone": "7701", "items": []}).startswith("h:"))   # без ключа — как раньше


if __name__ == "__main__":
    unittest.main()
