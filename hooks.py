"""Куда смотрят вебхуки (переезд на Hoster.kz, 04.11.2026). При запуске сервиса — в журнал только хосты адресов
(путь содержит секрет — его не пишем, токены тоже). Вебхук Telegram-бота, если он на *.onrender.com, переводится
на PUBLIC_URL (https://api.amura.kz) с тем же путём и теми же настройками: api.amura.kz сейчас смотрит на Render,
после смены DNS — на сервер, бот работает без перерыва. Так же подписки Meta (WhatsApp, Instagram) на *.onrender.com:
подписка приложения — тем же объектом и полями на новый адрес (ключ приложения из настроек WhatsApp), у аккаунтов WhatsApp
(основной номер и дополнительные) — override_callback_uri их же токенами. Meta проверяет новый адрес запросом — его
принимает тот же сервис. Без окна Facebook и без участия владельца."""
import json
from urllib.parse import urlparse

import requests

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
    import wa
    c = wa.cfg()
    if not (c.get("app_id") and c.get("secret")):
        return "WhatsApp/Instagram: приложение Meta не настроено"
    r = requests.get(f"{wa.GRAPH}/{c['app_id']}/subscriptions", timeout=20,
                     params={"access_token": f"{c['app_id']}|{c['secret']}"})
    subs = (r.json() or {}).get("data") or []
    new_host, out = host(oh.PUBLIC_URL), []
    for s in subs:
        old = s.get("callback_url") or ""
        if not host(old).endswith(".onrender.com") or not new_host or new_host.endswith(".onrender.com"):
            out.append(f"{s.get('object')} → {host(old)}")
            continue
        p = urlparse(old)
        new = oh.PUBLIC_URL.rstrip("/") + p.path + (f"?{p.query}" if p.query else "")
        fields = ",".join(f.get("name", "") if isinstance(f, dict) else str(f) for f in s.get("fields") or []) or "messages"
        j = requests.post(f"{wa.GRAPH}/{c['app_id']}/subscriptions", timeout=30, data={
            "object": s.get("object"), "callback_url": new, "verify_token": oh.HOOK_SECRET, "fields": fields,
            "access_token": f"{c['app_id']}|{c['secret']}"}).json()
        ok = bool(j.get("success"))
        if s.get("object") == "whatsapp_business_account":
            ok = waba_override(c, new) and ok
        out.append(f"{s.get('object')}: {host(old)} → {new_host}" + ("" if ok else " — НЕ ВЫШЛО: " + str((j.get("error") or {}).get("message", ""))[:120]))
    return "; ".join(out) or "Meta: подписок нет (Instagram и WhatsApp не подключены)"


def waba_override(c, new):
    """Адрес у каждого аккаунта WhatsApp (основной номер и дополнительные) — его же токеном."""
    import wa
    seen, ok = set(), True
    for tok, waba in accounts(c):
        if waba in seen:
            continue
        seen.add(waba)
        r = requests.post(f"{wa.GRAPH}/{waba}/subscribed_apps", timeout=30, headers={"Authorization": f"Bearer {tok}"},
                          json={"override_callback_uri": new, "verify_token": oh.HOOK_SECRET})
        if r.status_code >= 400:
            ok = False
            print(f"Вебхуки: аккаунт WhatsApp …{str(waba)[-4:]} — адрес не обновлён (HTTP {r.status_code})", flush=True)
    return ok


def accounts(c=None):
    import wa
    c = c or wa.cfg()
    rows = [(c.get("token"), c.get("waba"), "основной")] + [(x.get("token"), x.get("waba"), x.get("name") or "доп.") for x in wa.extra_lines()]
    skipped = [n for t, w, n in rows if not (t and w)]
    if skipped:
        print(f"Вебхуки: у номеров WhatsApp без сохранённого аккаунта (WABA) адрес не проверить: {', '.join(skipped)}", flush=True)
    return [(t, w) for t, w, _ in rows if t and w]


def waba_check():
    """Каждый аккаунт WhatsApp отдельно: куда смотрит его override_callback_uri; на *.onrender.com — переводим на PUBLIC_URL."""
    import wa
    new_host, out, seen = host(oh.PUBLIC_URL), [], set()
    for tok, waba in accounts():
        if waba in seen:
            continue
        seen.add(waba)
        h = {"Authorization": f"Bearer {tok}"}
        apps = requests.get(f"{wa.GRAPH}/{waba}/subscribed_apps", timeout=20, headers=h).json().get("data") or []
        uri = next((a.get("override_callback_uri") for a in apps if a.get("override_callback_uri")), "")
        tag = f"WABA …{str(waba)[-4:]}"
        if uri and host(uri).endswith(".onrender.com") and new_host and not new_host.endswith(".onrender.com"):
            p = urlparse(uri)
            new = oh.PUBLIC_URL.rstrip("/") + p.path + (f"?{p.query}" if p.query else "")
            r = requests.post(f"{wa.GRAPH}/{waba}/subscribed_apps", timeout=30, headers=h,
                              json={"override_callback_uri": new, "verify_token": oh.HOOK_SECRET})
            out.append(f"{tag}: {host(uri)} → {new_host}" + ("" if r.status_code < 400 else f" — НЕ ВЫШЛО (HTTP {r.status_code})"))
        else:
            out.append(f"{tag} → {host(uri) or 'адрес приложения'}" + ("" if apps else " (приложение не подписано)"))
    return "WhatsApp аккаунты: " + ("; ".join(out) or "нет")


def check():
    for name, fn in (("tg", telegram), ("meta", meta), ("meta", waba_check)):
        try:
            print("Вебхуки:", fn(), flush=True)
        except Exception as e:                       # без токенов в журнале: у Meta в тексте ошибки бывает адрес с ключом — только тип
            why = e.__class__.__name__ + ("" if name == "meta" else ": " + str(e)[:120].replace(oh.BOT or chr(0), "***"))
            print(f"Вебхуки: {name} — не проверено ({why})", flush=True)
