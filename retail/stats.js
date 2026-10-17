/* Аналитика карточек (модуль «Аналитика» в системе AMURA): обезличенные события розничного сайта — заход, просмотр карточки,
   добавление в корзину, оформление. Без cookies и без персональных данных: случайный номер посещения живёт в sessionStorage
   до закрытия вкладки. События копятся и уходят одной пачкой раз в 8 секунд и при уходе со страницы (sendBeacon). Только розница. */
(function(){
if(!window.AMURA_RETAIL || DEMO) return;
const URL_EV = CONFIG.apiUrl + "/an/ev";
let sid = ""; try{ sid = sessionStorage.getItem("amura-an") || ""; if(!sid){ sid = Math.random().toString(36).slice(2, 12) + Date.now().toString(36); sessionStorage.setItem("amura-an", sid); } }catch{ sid = Math.random().toString(36).slice(2, 14); }
sid = sid.replace(/[^a-z0-9]/g, "").slice(0, 32);
const seen = new Set();      // одна карточка / одна корзина на товар за посещение
let q = [["s"]], tm = 0;
function push(e, once){ if(once){ if(seen.has(once)) return; seen.add(once); } q.push(e); clearTimeout(tm); tm = setTimeout(flush, 8000); }
function flush(){
  clearTimeout(tm); if(!q.length) return;
  const body = JSON.stringify({ sid, src: (typeof REF !== "undefined" && REF.code && REF.code()) || "", ev: q }); q = [];
  try{ if(navigator.sendBeacon && navigator.sendBeacon(URL_EV, new Blob([body], { type: "text/plain" }))) return; }catch{}
  fetch(URL_EV, { method: "POST", body, headers: { "Content-Type": "text/plain" }, keepalive: true }).catch(() => {});
}
tm = setTimeout(flush, 3000);
document.addEventListener("visibilitychange", () => { if(document.visibilityState === "hidden") flush(); });
addEventListener("pagehide", flush);
window.AN = { ev: (k, id) => push(id ? [k, id] : [k], id ? k + id : k === "k" ? "k" : "") };

const _openA = openProduct;   // просмотр карточки
openProduct = function(it){ const r = _openA.apply(this, arguments); if(it && it.id) AN.ev("v", it.id); return r; };
const _saveA = save;          // корзина: товар появился в корзине (было 0 — стало больше)
let before = { ...cart };
save = function(k, v){
  if(k === "amura-cart2" && v) Object.keys(v).forEach(id => { if(!(before[id] > 0) && v[id] > 0) AN.ev("c", id); });
  if(k === "amura-cart2") before = { ...(v || {}) };
  return _saveA.apply(this, arguments);
};
const _fetchA = fetch;        // оформление: отправка заказа
fetch = function(u, o){ try{ if(typeof u === "string" && u === CONFIG.orderUrl && o && o.method === "POST") AN.ev("k"); }catch{} return _fetchA.apply(this, arguments); };
})();
