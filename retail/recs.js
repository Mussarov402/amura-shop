/* Розничный сайт: товары на каждом экране, как на WB. Подборка одна — window.recsFor (retail/hits.js): хиты,
   потом личное по истории в браузере, потом популярное; на каждом экране без того, что уже на нём.
   - товар: «Похожие товары» (тот же бренд или группа) и «Вам может понравиться» ниже описания и отзывов;
   - «Избранное», «Корзина» (и «Спасибо за заказ»), «Я» (и «Заказ оплачен») — «Вам может понравиться» внизу.
   Карточки как в каталоге, подгрузка по 12 при прокрутке. Только розница. */
(function(){
if(!window.AMURA_RETAIL || !window.recsFor) return;
const STEP = 12, MAX = 240, SIMILAR = 12;

function block(title, items, opts = {}){
  const sec = document.createElement("section");
  sec.className = "rrec-sec" + (opts.cls ? " " + opts.cls : "");
  sec.innerHTML = `<h2 class="rrec-h">${esc(title)}</h2><div class="grid rrec"></div><div class="rrec-more" aria-hidden="true"></div>`;
  const g = sec.querySelector(".rrec"), L = items.slice(0, opts.max || MAX);
  let n = 0, io = null;
  const more = () => {
    if(n >= L.length) return;
    g.insertAdjacentHTML("beforeend", L.slice(n, n + STEP).map(cardHTML).join("")); n += STEP;
    if(n >= L.length && io) io.disconnect();
  };
  more();
  if(L.length > STEP){ io = new IntersectionObserver(es => { if(es.some(e => e.isIntersecting)) more(); }, { root: opts.root || null, rootMargin: "600px" }); io.observe(sec.querySelector(".rrec-more")); }
  if(!opts.root){ g.addEventListener("click", onBuyClick); g.addEventListener("change", onBuyChange); }   // в окне товара клики ловит #sheet
  return sec;
}
const MAY = "Вам может понравиться";

/* товар: похожие и подборка — в конце страницы товара */
const _open = openProduct;
openProduct = function(it){
  const r = _open.apply(this, arguments);
  const sh = $("#sheet");
  if(it && sh && ALL.length){
    const sim = ALL.filter(x => x.id !== it.id && (x.brand === it.brand || (it.group && x.group === it.group)))
      .sort((a, b) => (b.brand === it.brand) - (a.brand === it.brand) || popRank(a) - popRank(b)).slice(0, SIMILAR);
    if(sim.length) sh.appendChild(block("Похожие товары", sim, { root: sh, cls: "insheet" }));
    sh.appendChild(block(MAY, recsFor([it.id, ...sim.map(x => x.id)]), { root: sh, cls: "insheet" }));
  }
  return r;
};

/* экраны «Избранное», «Корзина», «Я»: блок после содержимого; пересобираем, только когда изменилось то, что исключаем */
function place(host, key, exclude, cls){
  if(!host || !ALL.length) return;
  const at = host.closest(".narrow") || host;     // корзина и кабинет — узкая колонка; подборка после неё во всю ширину, как на главной (5 в ряд)
  let sec = at.nextElementSibling && at.nextElementSibling.classList.contains("rrec-sec") ? at.nextElementSibling : null;
  if(key === null){ if(sec) sec.remove(); return; }
  if(sec && sec.dataset.key === key) return;
  const nb = block(MAY, recsFor(exclude), { cls: at === host ? cls : (cls ? cls + " " : "") + "wrap" });
  nb.dataset.key = key;
  if(sec) sec.replaceWith(nb); else at.after(nb);
}
const ids = o => Object.keys(o).sort().join(",");
function favRecs(){
  const note = $("#favNote");
  if(!FAV.length && note) note.innerHTML = '<p class="kv rrec-empty">В избранном пока ничего нет</p>';
  place($("#favGrid"), "f:" + FAV.join(","), FAV);
}
function cartRecs(){
  const body = $("#drawerBody"); if(!body) return;
  const checkout = body.parentElement.classList.contains("rstep2") && !body.querySelector(".rdone");   // оформление — без подборки
  const done = body.querySelector(".rdone");
  place(body, checkout ? null : (done ? "d:" : "c:") + ids(cart), Object.keys(cart), "incart");
}
function meRecs(){ const ex = window.moExclude ? window.moExclude() : null; place($("#meBody"), ex ? "o:" + ex.join(",") : "m", ex || [], "inme"); }   // «Мои заказы» — без товаров, что ещё едут
const NOW = { fav: favRecs, cart: cartRecs, me: meRecs };

const _fav = renderFav;
renderFav = function(){ const r = _fav.apply(this, arguments); favRecs(); return r; };
["#drawerBody", "#meBody"].forEach(sel => { const el = $(sel); if(el) new MutationObserver(() => { if(VIEW === "cart" || VIEW === "me") NOW[VIEW](); }).observe(el, { childList: true }); });
const _go = go;
go = function(v){ const r = _go.apply(this, arguments); if(NOW[v]) NOW[v](); return r; };
const _render = render;      // каталог загрузился позже, чем открыли экран, — достроить подборку
render = function(){ const r = _render.apply(this, arguments); if(NOW[VIEW]) NOW[VIEW](); return r; };
/* в корзине добавили товар из подборки — он сразу появляется в списке */
document.addEventListener("click", e => {
  if(VIEW === "cart" && e.target.closest(".rrec-sec.incart [data-radd]")) setTimeout(() => renderCart(), 0);
});
})();
