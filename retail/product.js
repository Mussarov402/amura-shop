/* Розничный сайт: товар открывается отдельной страницей (как на WB), со своей ссылкой amura.kz/shop/#p/<id>.
   Кнопка «Назад» телефона/браузера возвращает в каталог на то же место; ссылкой можно поделиться.
   Внутри — то же окно товара (#sheet): цены, корзина, описание, отзывы работают как раньше. Только розница. */
(function(){
if(!window.AMURA_RETAIL) return;
const PH = "#p/", TITLE = document.title;
const CART_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 8h14l-1 12H6L5 8Z"/><path d="M9 8V6.5a3 3 0 0 1 6 0V8"/></svg>';
const SHARE_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15V4M7.5 8.5 12 4l4.5 4.5"/><path d="M5 13v6h14v-6"/></svg>';
const isPage = () => document.body.classList.contains("rpage");
const cartN = () => Object.values(cart).reduce((s, q) => s + q, 0);

function rateLine(id){
  const r = (window.RV_SUM || {})[id]; if(!r || !r[1]) return "";
  const n = r[1], m = n % 10, h = n % 100, w = m === 1 && h !== 11 ? "оценка" : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? "оценки" : "оценок";
  return `<button type="button" class="rp-rate" data-rprate><b>★ ${String(r[0]).replace(".", ",")}</b> · ${fmt(n)} ${w}</button>`;
}

const _open = openProduct;
openProduct = function(it, fromHistory){
  _open(it);
  const sh = $("#sheet"), n = cartN();
  document.body.classList.add("rpage"); $("#scrim").classList.remove("open");
  sh.insertAdjacentHTML("afterbegin", `<div class="rp-bar">
    <button type="button" class="rp-back" data-rpback aria-label="Назад"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></button>
    <span class="rp-t">${esc(it.brand || "Товар")}</span>
    <button type="button" class="rp-ic" data-rpshare aria-label="Поделиться">${SHARE_SVG}</button>
    <button type="button" class="rp-ic" data-rpcart aria-label="Корзина">${CART_SVG}<em id="rpCnt" ${n ? "" : "hidden"}>${n}</em></button></div>`);
  const h3 = sh.querySelector(".info h3"); if(h3) h3.insertAdjacentHTML("afterend", rateLine(it.id));
  sh.scrollTop = 0;
  const act = sh.querySelector(".act"); if(act) sh.style.setProperty("--rpact", act.offsetHeight + "px");   // телефон: место под закреплённой кнопкой покупки
  document.title = it.name + " — AMURA";
  const h = PH + encodeURIComponent(it.id);
  if(!fromHistory && location.hash !== h){
    try{ if(location.hash.startsWith(PH)) history.replaceState({ rp: 1 }, "", h); else history.pushState({ rp: 1 }, "", h); }catch{}
  }
};

const _close = closeAll;
closeAll = function(){
  const was = isPage();
  _close();
  if(!was) return;
  document.body.classList.remove("rpage"); document.title = TITLE;
  if(location.hash.startsWith(PH)) try{ history.replaceState(null, "", location.pathname); }catch{}
};

const _cnt = renderCartCount;
renderCartCount = function(){ _cnt(); const c = $("#rpCnt"); if(c){ const n = cartN(); c.textContent = n; c.hidden = !n; } };

$("#sheet").addEventListener("click", e => {
  if(!isPage()) return;
  if(e.target.closest("[data-rpback]")){
    if(history.state && history.state.rp) history.back(); else closeAll();
    return;
  }
  if(e.target.closest("[data-rpcart]")) return go("cart");
  if(e.target.closest("[data-rprate]")){ const b = $("#rvBox"); if(b) b.scrollIntoView({ behavior: "smooth", block: "start" }); return; }
  if(e.target.closest("[data-rpshare]")){
    const url = location.href, title = document.title;
    if(navigator.share) navigator.share({ title, url }).catch(() => {});
    else if(navigator.clipboard) navigator.clipboard.writeText(url).then(() => toast("Ссылка скопирована"), () => {});
  }
});

addEventListener("popstate", () => {
  const h = location.hash;
  if(h.startsWith(PH)){ const it = byId(decodeURIComponent(h.slice(PH.length))); if(it) openProduct(it, true); }
  else if(isPage()) closeAll();
});

// открыли ссылку на товар: каталог грузится асинхронно — ждём товар, под ним в истории оставляем каталог (назад — в каталог)
if(location.hash.startsWith(PH)){
  const id = decodeURIComponent(location.hash.slice(PH.length)), t0 = Date.now();
  try{ history.replaceState(null, "", location.pathname); }catch{}
  const tick = setInterval(() => {
    const it = byId(id);
    if(it){ clearInterval(tick); openProduct(it); }
    else if(Date.now() - t0 > 20000) clearInterval(tick);
  }, 200);
}
})();
