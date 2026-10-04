// AMURA: сайт открывается мгновенно, как приложение.
// Страница и фото — из памяти телефона сразу, в фоне обновляются.
// Живые остатки, заказы и вход (сервер) — только из сети, не кэшируются.
const V = "amura-v35";
self.addEventListener("install", e => { self.skipWaiting(); e.waitUntil(caches.open(V).then(c => c.addAll(["./", "index.html", "manifest.webmanifest", "icon-192.png", "icon-180.png"]))); });
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
  // страница: сначала свежая из сети (до 3 секунд), иначе из памяти — обновления видны сразу
  e.respondWith(caches.open(V).then(c => {
    const net = fetch(req, { cache: "no-cache" }).then(r => { if(r.ok) c.put(req, r.clone()); return r; });
    const slow = new Promise(res => setTimeout(res, 3000)).then(() => c.match(req));
    return Promise.race([net, slow.then(h => h || net)]).catch(() => c.match(req));
  }));
});
