/* Розничный сайт: «назад» как на WB и Ozon.
   - Товар открыт из каталога, бренда или главной → «назад» (свайп, «<» в шапке товара, кнопка браузера) — к той же карточке
     на том же месте; лента «Подобрали для вас» та же (retail/hits.js держит порядок всю сессию).
   - Поиск: в истории одна запись на весь поиск (поле, подсказки, результаты). Товар из поиска встаёт на её место,
     поэтому «назад» и с товара, и со страницы результатов — на главную, туда, где были до поиска; поле очищается.
   - Safari выгрузил вкладку и открыл заново — возвращаем подгруженные страницы и место (sessionStorage).
   - Картинки карточек грузим заранее, за 1–2 экрана до появления, чтобы при быстрой прокрутке не было белых карточек.
   Только розница. */
(function(){
if(!window.AMURA_RETAIL) return;
const q = $("#q"), isPage = () => document.body.classList.contains("rpage");
let preY = null;          // где был покупатель на главной до поиска
let fromSearch = false;   // открытый товар — из поиска

q.addEventListener("focus", () => { if(preY === null && VIEW === "home" && !st.q && !isPage()) preY = scrollY; });

function clearSearch(){
  q.value = ""; if(window.qSync) qSync();
  if(st.q){ st.q = ""; st.shown = PAGE; render(); }
  const y = preY || 0; preY = null;
  scrollTo(0, y); requestAnimationFrame(() => scrollTo(0, y));
}

/* поиск: одна запись в истории */
$("#searchForm").addEventListener("submit", () => setTimeout(() => {
  if(st.q && !isPage() && !(history.state && history.state.rs)) try{ history.pushState({ rs: 1 }, "", "#s"); }catch{}
}, 0));

const _open = openProduct;
openProduct = function(it, fromHistory){
  if(!fromHistory && !isPage()) fromSearch = !!(st.q || q.value.trim() || (history.state && history.state.rs));
  return _open.apply(this, arguments);
};
const _close = closeAll;
closeAll = function(){
  const was = isPage();
  const r = _close.apply(this, arguments);
  if(was && fromSearch){ fromSearch = false; clearSearch(); }
  return r;
};
addEventListener("popstate", () => {          // «назад» со страницы результатов
  if(!isPage() && !(history.state && history.state.rs) && (st.q || q.value) && VIEW === "home") clearSearch();
});
$("#qx").addEventListener("click", () => { if(!st.q) preY = null; });

/* Safari выгрузил вкладку — вернуть подгруженное и место */
const POS = "amura-pos";
addEventListener("pagehide", () => {
  const y = isPage() ? -parseInt(document.body.style.top || "0", 10) : scrollY;
  try{ sessionStorage.setItem(POS, JSON.stringify({ y, shown: st.shown, q: st.q, brand: st.brand, view: VIEW, t: Date.now() })); }catch{}
});
(() => {
  const nav = performance.getEntriesByType && performance.getEntriesByType("navigation")[0];
  if(!nav || nav.type !== "back_forward" || location.hash) return;
  let p = null; try{ p = JSON.parse(sessionStorage.getItem(POS)); }catch{}
  if(!p || Date.now() - p.t > 30 * 60000 || p.view !== "home" || p.q) return;
  const t0 = Date.now(), tick = setInterval(() => {
    if(!ALL.length){ if(Date.now() - t0 > 15000) clearInterval(tick); return; }
    clearInterval(tick);
    if(p.brand && st.brand !== p.brand && window.selectBrand) selectBrand(p.brand);
    st.shown = Math.max(st.shown, p.shown || PAGE); render();
    scrollTo(0, p.y); requestAnimationFrame(() => scrollTo(0, p.y));
  }, 100);
})();

/* картинки — заранее, за 1–2 экрана (Safari сам начинает слишком поздно) */
const io = "IntersectionObserver" in window ? new IntersectionObserver(es => es.forEach(e => {
  if(e.isIntersecting){ e.target.loading = "eager"; io.unobserve(e.target); }
}), { rootMargin: "1600px 0px" }) : null;
const watch = root => { if(io && root.querySelectorAll) root.querySelectorAll('.thumb img[loading="lazy"]').forEach(i => io.observe(i)); };
if(io){
  watch(document);
  new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => { if(n.nodeType === 1) watch(n); })))
    .observe(document.body, { childList: true, subtree: true });
}
})();
