"""Куда смотрят вебхуки (переезд на Hoster.kz, 04.11.2026). При запуске сервиса — в журнал только хосты адресов
(путь содержит секрет — его не пишем, токены тоже). Вебхук Telegram-бота, если он на *.onrender.com, переводится
на PUBLIC_URL (https://api.amura.kz) с тем же путём и теми же настройками: api.amura.kz сейчас смотрит на Render,
после смены DNS — на сервер, бот работает без перерыва. WhatsApp и Instagram только показываем: их переподключает владелец."""
import json
from urllib.parse import urlparse

import order_hook as oh


def host(url):
    return (urlparse(url or "").hostname or "").lower()


def telegram():
    info = (oh.tg("getWebhookInfo") or {}).get("result") or {}
    url = info.get("url", "")
    new_host = host(oh.PUBLIC_URL)
    if not url or not host(url).endswith(".onrender.com") or not new_host or new_host.endswith(".onrender.com"):
        return f"Telegram → {host(url) or 'нет вебхука'}"
    p = urlparse(url)
    new = oh.PUBLIC_URL.rstrip("/") + p.path + (f"?{p.query}" if p.query else "")
    data = {"url": new, "max_connections": info.get("max_connections") or 40}
    if info.get("allowed_updates"):
        data["allowed_updates"] = json.dumps(info["allowed_updates"])
    oh.tg("setWebhook", **data)
    return f"Telegram: {host(url)} → {new_host} (путь прежний)"


def meta():
    import requests
    import wa
    c = wa.cfg()
    if not (c.get("app_id") and c.get("secret")):
        return "WhatsApp/Instagram: приложение Meta не настроено"
    r = requests.get(f"{wa.GRAPH}/{c['app_id']}/subscriptions", timeout=20,
                     params={"access_token": f"{c['app_id']}|{c['secret']}"})
    subs = (r.json() or {}).get("data") or []
    return "; ".join(f"{s.get('object')} → {host(s.get('callback_url'))}" for s in subs) or "Meta: подписок нет"


def check():
    for name, fn in (("tg", telegram), ("meta", meta)):
        try:
            print("Вебхуки:", fn(), flush=True)
        except Exception as e:                       # без токенов в журнале: у Meta в тексте ошибки бывает адрес с ключом — только тип
            why = e.__class__.__name__ + ("" if name == "meta" else ": " + str(e)[:120].replace(oh.BOT or chr(0), "***"))
            print(f"Вебхуки: {name} — не проверено ({why})", flush=True)
