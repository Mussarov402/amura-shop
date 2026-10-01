// AMURA: сайт открывается мгновенно, как приложение.
// Страница и фото — из памяти телефона сразу, в фоне обновляются.
// Живые остатки, заказы и вход (сервер) — только из сети, не кэшируются.
const V = "amura-v1";
self.addEventListener("install", e => { self.skipWaiting(); e.waitUntil(caches.open(V).then(c => c.addAll(["./", "index.html", "manifest.webmanifest", "icon-192.png"]))); });
self.addEventListener("activate", e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== V).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if(req.method !== "GET") return;
  const same = url.origin === location.origin, fonts = /fonts\.(googleapis|gstatic)\.com$/.test(url.hostname);
  if(!same && !fonts) return;                         // сервер Render — напрямую
  if(same && url.pathname.endsWith("catalog.json")) return;   // каталог свежий из сети (страница сама кэширует)
  if(same && url.pathname.includes("/img/") || fonts){        // фото и шрифты: из памяти, если есть
    e.respondWith(caches.open(V).then(c => c.match(req).then(hit => hit || fetch(req).then(r => { if(r.ok || r.type === "opaque") c.put(req, r.clone()); return r; }))));
    return;
  }
  // страница: сразу из памяти, в фоне — новая версия
  e.respondWith(caches.open(V).then(c => c.match(req).then(hit => {
    const net = fetch(req).then(r => { if(r.ok) c.put(req, r.clone()); return r; }).catch(() => hit);
    return hit || net;
  })));
});
