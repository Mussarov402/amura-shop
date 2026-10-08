/* Розничный сайт: карточки товаров как на WB и «полёт» товара в корзину.
   Крупное фото, ♡, круглая кнопка корзины на фото (число — сколько уже в корзине), цена, бренд и название.
   Нажатие на кнопку — +1 шт в корзину, фото товара дугой улетает к значку «Корзина». Только розница. */
(function(){
if(!window.AMURA_RETAIL) return;
const CART_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 8h14l-1 12H6L5 8Z"/><path d="M9 8V6.5a3 3 0 0 1 6 0V8"/></svg>';

cardHTML = function(it){
  const q = cart[it.id] || 0;
  return `<article class="card rpc" data-id="${esc(it.id)}">
    <div class="rpc-img">${favBtnHTML(it.id)}
      <button class="thumb" data-open aria-label="${esc(it.name)}">${it.img ? `<img src="${esc(it.img)}" alt="" loading="lazy" decoding="async" width="500" height="500">` : `<span class="ph">${esc((it.brand || it.name || "A").charAt(0))}</span>`}${it.isNew ? '<span class="tag">Новинка</span>' : ""}</button>
      <button type="button" class="rpc-add${q ? " in" : ""}" data-radd aria-label="${q ? `В корзине ${q} шт, добавить ещё` : "В корзину"}">${CART_SVG}${q ? `<em>${q}</em>` : ""}</button>
    </div>
    <div class="rpc-price">${fmt(unitPrice(it, 1))} ₸</div>
    <button type="button" class="rpc-name" data-open>${it.brand && it.brand.length <= 20 ? `<b>${esc(it.brand)}</b> / ` : ""}${esc(it.name)}</button>
    ${it.qty <= 5 ? `<div class="rpc-low">Осталось ${fmt(it.qty)} шт</div>` : ""}
  </article>`;
};

const _refresh = refreshCard;
refreshCard = function(id){
  _refresh(id);
  const q = cart[id] || 0;
  document.querySelectorAll(`.rpc[data-id="${CSS.escape(id)}"] .rpc-add`).forEach(b => {
    b.classList.toggle("in", !!q); b.innerHTML = CART_SVG + (q ? `<em>${q}</em>` : "");
    b.setAttribute("aria-label", q ? `В корзине ${q} шт, добавить ещё` : "В корзину");
  });
};

function cartTarget(){
  return [...document.querySelectorAll('.tabbar [data-view="cart"], .topnav [data-view="cart"]')]
    .map(b => b.querySelector(".ic") || b).find(e => { const r = e.getBoundingClientRect(); return r.width && getComputedStyle(e.closest("nav")).display !== "none"; });
}
function bump(t, delay){ if(t) t.animate([{ transform: "scale(1)" }, { transform: "scale(1.3)" }, { transform: "scale(1)" }], { duration: 380, delay: delay || 0, easing: "ease-out" }); }
/* фото летит дугой к значку корзины, уменьшаясь; исходное фото на миг бледнеет */
function fly(src){
  const t = cartTarget();
  if(!src || !t || matchMedia("(prefers-reduced-motion: reduce)").matches) return bump(t);
  const a = src.getBoundingClientRect(), b = t.getBoundingClientRect();
  const size = Math.max(48, Math.min(a.width, a.height, 150));
  const x0 = a.left + a.width / 2, y0 = a.top + a.height / 2, dx = b.left + b.width / 2 - x0, dy = b.top + b.height / 2 - y0;
  const outer = document.createElement("div"), inner = src.tagName === "IMG" ? src.cloneNode() : document.createElement("div");
  outer.className = "rfly"; inner.className = "rfly-i" + (src.tagName === "IMG" ? "" : " ph");
  if(src.tagName !== "IMG") inner.textContent = src.textContent;
  outer.style.cssText = `left:${x0 - size / 2}px;top:${y0 - size / 2}px;width:${size}px;height:${size}px`;
  outer.appendChild(inner); document.body.appendChild(outer);
  const dur = 720, lift = Math.min(90, Math.abs(dy) * .35 + 30);
  outer.animate([{ transform: "translateX(0)" }, { transform: `translateX(${dx}px)` }], { duration: dur, easing: "cubic-bezier(.45,0,.55,1)", fill: "forwards" });
  inner.animate([
    { transform: "translateY(0) scale(1)", opacity: 1 },
    { transform: `translateY(${-lift}px) scale(.9)`, opacity: 1, offset: .3 },
    { transform: `translateY(${dy}px) scale(.16)`, opacity: .55 }
  ], { duration: dur, easing: "cubic-bezier(.4,0,.7,1)", fill: "forwards" }).onfinish = () => outer.remove();
  src.animate([{ opacity: .2 }, { opacity: .2, offset: .4 }, { opacity: 1 }], { duration: dur });
  bump(t, dur - 80);
}

function addOne(it, from){
  const q = (cart[it.id] || 0) + 1;
  if(q > it.qty) return toast("Больше нет в наличии");
  cart[it.id] = q; save("amura-cart2", cart); renderCartCount(); refreshCard(it.id);
  fly(from);
}
["#grid", "#favGrid"].forEach(sel => $(sel).addEventListener("click", e => {
  const btn = e.target.closest("[data-radd]"); if(!btn) return;
  const card = btn.closest(".rpc"), it = card && byId(card.dataset.id); if(!it) return;
  addOne(it, card.querySelector(".thumb img") || card.querySelector(".thumb .ph"));
}));
/* окно товара: «В корзину» — тоже полёт */
$("#sheet").addEventListener("click", e => {
  if(!e.target.closest("[data-add]")) return;
  const s = $("#sheet"); if(cart[s.dataset.id]) fly(s.querySelector(".pic img") || s.querySelector(".pic .ph"));   // кнопку уже перерисовали — окно берём напрямую
});

if(typeof render === "function" && ALL.length) render();
})();
