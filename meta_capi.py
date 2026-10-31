"""Meta Conversions API: покупка с сайта с сервера (тот же event_id, что у пикселя на сайте — номер заказа, Meta уберёт дубль).
Включается только переменными Render META_PIXEL_ID и META_CAPI_TOKEN (токен владелец кладёт сам); без них ничего не отправляем.
Только при согласии посетителя на cookie рекламы (флаг adsConsent из браузера с заказом — order_hook.py).
Личных данных (телефон, имя, адрес) не передаём: только IP и браузер покупателя, куки пикселя _fbp/_fbc, если сайт их прислал."""
import os
import time

import requests

GRAPH = "https://graph.facebook.com/v21.0"


def configured():
    return bool(os.environ.get("META_PIXEL_ID", "").strip() and os.environ.get("META_CAPI_TOKEN", "").strip())


def purchase_event(number, total, items, site, ip="", ua="", url="", fbp="", fbc=""):
    """Событие Purchase в формате Conversions API."""
    user = {k: v for k, v in (("client_ip_address", ip), ("client_user_agent", ua), ("fbp", fbp), ("fbc", fbc)) if v}
    return {"event_name": "Purchase", "event_time": int(time.time()), "event_id": str(number), "action_source": "website",
            **({"event_source_url": url} if url else {}), "user_data": user,
            "custom_data": {"currency": "KZT", "value": round(float(total or 0)), "content_type": "product",
                            "content_ids": [str(i.get("id")) for i in items if i.get("id")],
                            "num_items": sum(int(i.get("qty") or 0) for i in items), "site": site}}


def send_purchase(**kw):
    """Отправить покупку в Meta. Нет переменных — ничего не делаем (False). Ошибка Meta — в лог, заказу не мешает."""
    if not configured():
        return False
    pid, tok = os.environ["META_PIXEL_ID"].strip(), os.environ["META_CAPI_TOKEN"].strip()
    try:
        r = requests.post(f"{GRAPH}/{pid}/events", params={"access_token": tok}, json={"data": [purchase_event(**kw)]}, timeout=20)
        if r.status_code >= 400:
            print("Meta CAPI: покупка не принята:", r.status_code, r.text[:300], flush=True)
            return False
        return True
    except requests.RequestException as e:
        print("Meta CAPI: нет связи:", e.__class__.__name__, flush=True)
        return False
