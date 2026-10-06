"""Push-уведомления панели (Web Push): звук и счётчик на иконке приложения, даже когда панель закрыта.
Работает в Chrome/Edge/Android и на iPhone (iOS 16.4+), если панель добавлена на экран «Домой».
Ключи VAPID создаются один раз и хранятся в базе (таблица setting), подписки — в таблице push_sub."""
import base64
import json
import threading
import time

import inbox
import order_hook as oh
import team

_keys = {}
_ready = [False]


def _db():
    d = inbox.db()
    if not _ready[0]:
        d.run("CREATE TABLE IF NOT EXISTS push_sub (endpoint TEXT PRIMARY KEY, sub TEXT, chat TEXT, at DOUBLE PRECISION)")
        d.c.commit()
        _ready[0] = True
    return d


def keys():
    """(приватный, публичный) ключ VAPID в base64url; при первом вызове создаются и сохраняются в базе."""
    if _keys:
        return _keys["priv"], _keys["pub"]
    with _db() as d:
        priv, pub = inbox.get_setting(d, "vapid_priv", ""), inbox.get_setting(d, "vapid_pub", "")
        if not priv:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            k = ec.generate_private_key(ec.SECP256R1())
            b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
            priv = b64(k.private_numbers().private_value.to_bytes(32, "big"))
            pub = b64(k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
            inbox.set_setting(d, "vapid_priv", priv)
            inbox.set_setting(d, "vapid_pub", pub)
    _keys.update(priv=priv, pub=pub)
    return priv, pub


def subscribe(sub, chat):
    ep = str((sub or {}).get("endpoint", ""))
    if not ep.startswith("https://") or not (sub.get("keys") or {}).get("p256dh"):
        raise ValueError("Некорректная подписка")
    with _db() as d:
        d.run("DELETE FROM push_sub WHERE endpoint=%s", (ep,))
        d.run("INSERT INTO push_sub (endpoint, sub, chat, at) VALUES (%s,%s,%s,%s)", (ep, json.dumps(sub), str(chat), time.time()))


def _allowed(chat, perm):
    if str(chat) == str(oh.OWNER):
        return True
    s = team.by_chat(chat)
    return bool(s and (not perm or perm in s["perms"]))


def unread():
    try:
        with inbox.db() as d:
            return int((d.run("SELECT COALESCE(SUM(unread),0) FROM conv", one=True) or [0])[0])
    except Exception:
        return 0


def _send(title, body, url, perm, tag, only_chat):
    try:
        from pywebpush import WebPushException, webpush
        priv, _ = keys()
        with _db() as d:
            rows = d.run("SELECT endpoint, sub, chat FROM push_sub", many=True)
        data = json.dumps({"title": title, "body": body[:180], "url": url, "tag": tag, "badge": unread()}, ensure_ascii=False)
        claims_sub = oh.PUBLIC_URL if str(oh.PUBLIC_URL).startswith("https://") else "https://admin.amura.kz"
        for ep, sub, chat in rows:
            if (only_chat and str(chat) != str(only_chat)) or not _allowed(chat, perm):
                continue
            try:
                webpush(subscription_info=json.loads(sub), data=data, vapid_private_key=priv,
                        vapid_claims={"sub": claims_sub}, ttl=6 * 3600, timeout=10)
            except WebPushException as e:
                code = getattr(getattr(e, "response", None), "status_code", 0)
                if code in (404, 410):                    # подписка больше не действует (удалили приложение, сбросили разрешение)
                    with _db() as d:
                        d.run("DELETE FROM push_sub WHERE endpoint=%s", (ep,))
                else:
                    print("Push не отправлен:", code, str(e)[:200], flush=True)
            except Exception as e:
                print("Push не отправлен:", str(e)[:200], flush=True)
    except Exception as e:
        print("Push:", str(e)[:200], flush=True)


def notify(title, body, url="/admin", perm="", tag="", only_chat=None):
    """Уведомление в фоне: владельцу и сотрудникам с правом perm (inbox / orders / products)."""
    threading.Thread(target=_send, args=(title, body or "", url, perm, tag, only_chat), daemon=True).start()


SW_JS = """// AMURA панель: push-уведомления и счётчик на иконке
self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("push", e => {
  let d = {};
  try { d = e.data.json(); } catch (_) { d = { title: "AMURA", body: e.data ? e.data.text() : "" }; }
  const jobs = [self.registration.showNotification(d.title || "AMURA", {
    body: d.body || "", icon: "/admin/icon/192", badge: "/admin/icon/192",
    tag: d.tag || undefined, renotify: !!d.tag, data: { url: d.url || "/admin" } })];
  if (typeof d.badge === "number" && self.navigator && self.navigator.setAppBadge)
    jobs.push((d.badge > 0 ? self.navigator.setAppBadge(d.badge) : self.navigator.clearAppBadge()).catch(() => {}));
  e.waitUntil(Promise.all(jobs));
});
self.addEventListener("notificationclick", e => {
  e.notification.close();
  const url = (e.notification.data || {}).url || "/admin";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(ws => {
    for (const w of ws) if (w.url.indexOf("/admin") >= 0) { w.postMessage({ open: url }); return w.focus(); }
    return self.clients.openWindow(url);
  }));
});
"""
