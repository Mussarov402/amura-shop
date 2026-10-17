/* Розничный сайт: «Хиты продаж» под баннерами (лента на телефоне, сетка на компьютере), сортировка «Популярные» по умолчанию
   и метка «Хит» на карточке. Рейтинг считает сервер (продажи за 30 дней, закреплённые и исключённые в системе AMURA). Только розница. */
(function(){
if(!window.AMURA_RETAIL) return;
let HITS = [], POP = new Map();
const HSET = () => new Set(HITS);

const _apply = applyData;   // в живом каталоге сервер присылает hits и pop; в статичном их нет — оставляем прежние
applyData = function(data){
  if(data && Array.isArray(data.pop)){ HITS = data.hits || []; POP = new Map(data.pop.map((id, i) => [id, i])); }
  const r = _apply.apply(this, arguments); drawHits(); return r;
};

/* сортировка «Популярные» — первой и по умолчанию */
const sel = $("#sort");
if(sel && !sel.querySelector('option[value="pop"]')){ sel.insertAdjacentHTML("afterbegin", '<option value="pop">Популярные</option>'); }
if(st.sort === "default"){ st.sort = "pop"; if(sel) sel.value = "pop"; }
const _filtered = filtered;
filtered = function(){
  const list = _filtered.apply(this, arguments);
  if(st.sort === "pop"){ const n = POP.size; list.sort((a, b) => (POP.has(a.id) ? POP.get(a.id) : n) - (POP.has(b.id) ? POP.get(b.id) : n) || (a.brand || "").localeCompare(b.brand || "", "ru") || a.name.localeCompare(b.name, "ru")); }
  return list;
};

/* метка «Хит» на карточке (CSS), без изменения разметки карточки */
const _card = cardHTML;
cardHTML = function(it){ const h = _card.apply(this, arguments); return HSET().has(it.id) ? h.replace('class="thumb" data-open', 'class="thumb" data-open data-hit') : h; };

/* лента «Хиты продаж» под баннерами */
const hero = document.querySelector("#viewHome .hero");
let box = null;
function drawHits(){
  const items = HITS.map(id => byId(id)).filter(Boolean).slice(0, 12);
  if(!hero) return;
  if(!items.length){ if(box) box.hidden = true; return; }
  if(!box){ box = document.createElement("section"); box.className = "rhits"; box.setAttribute("aria-label", "Хиты продаж"); hero.after(box);
    box.addEventListener("click", onClick); box.addEventListener("change", onBuyChange); }
  box.hidden = false;
  box.innerHTML = `<div class="wrap"><div class="rhits-h"><h2>Хиты продаж</h2><button type="button" class="rhits-all" data-hits-all>Все популярные</button></div>
    <div class="rhits-row grid">${items.map(cardHTML).join("")}</div></div>`;
}
function onClick(e){
  if(e.target.closest("[data-hits-all]")){ st.sort = "pop"; st.brand = ""; st.q = ""; if(sel) sel.value = "pop"; render(); document.querySelector("main .toolbar").scrollIntoView({ behavior: "smooth", block: "start" }); return; }
  const add = e.target.closest("[data-radd]");
  if(add){ const card = add.closest("[data-id]"), it = card && byId(card.dataset.id); if(!it) return;
    const q = (cart[it.id] || 0) + 1; if(q > it.qty) return toast("Больше нет в наличии");
    cart[it.id] = q; save("amura-cart2", cart); renderCartCount(); refreshCard(it.id); drawHits(); toast(`${it.name.slice(0, 40)} — в корзине`); return; }
  onBuyClick(e);
}
const _refresh = refreshCard;   // корзина/избранное поменялись — обновить и ленту
refreshCard = function(id){ const r = _refresh.apply(this, arguments); if(box && !box.hidden && HITS.includes(id) && !(box.contains(document.activeElement) && document.activeElement.matches("input"))) drawHits(); return r; };
drawHits();
})();
