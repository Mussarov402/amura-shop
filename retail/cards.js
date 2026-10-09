/* Розничный сайт: карточки товаров как на WB и «полёт» товара в корзину.
   Крупное фото, ♡, круглая кнопка корзины на фото (число — сколько уже в корзине), цена, бренд и название.
   Нажатие на кнопку — +1 шт в корзину, фото товара дугой улетает к значку «Корзина». Только розница. */
(function(){
if(!window.AMURA_RETAIL) return;
/* синяя галочка «оригинал» перед брендом, как у WB */
const VERIFIED = '<svg class="rpc-ok" viewBox="0 0 24 24" aria-label="Оригинальный товар" role="img"><path d="M12.00 1.60 L14.41 3.02 L17.20 2.99 L18.58 5.42 L21.01 6.80 L20.98 9.59 L22.40 12.00 L20.98 14.41 L21.01 17.20 L18.58 18.58 L17.20 21.01 L14.41 20.98 L12.00 22.40 L9.59 20.98 L6.80 21.01 L5.42 18.58 L2.99 17.20 L3.02 14.41 L1.60 12.00 L3.02 9.59 L2.99 6.80 L5.42 5.42 L6.80 2.99 L9.59 3.02Z" stroke-linejoin="round" stroke-width="1.6"/><path d="M8 12.3l2.6 2.6L16.2 9.3" fill="none" stroke="#fff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CART_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 8h14l-1 12H6L5 8Z"/><path d="M9 8V6.5a3 3 0 0 1 6 0V8"/></svg>';

/* оценка из отзывов (retail/reviews.js кладёт сводку в window.RV_SUM) */
function rateHTML(id){
  const r = (window.RV_SUM || {})[id]; if(!r || !r[1]) return "";
  const n = r[1], m = n % 10, h = n % 100, w = m === 1 && h !== 11 ? "оценка" : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? "оценки" : "оценок";
  return `<div class="rpc-rate"><b>★</b> ${String(r[0]).replace(".", ",")} <span>· ${fmt(n)} ${w}</span></div>`;
}
/* срок на кнопке «в корзину» (компьютер, как на WB): Алматы — Яндекс «в течение дня» (до 13:00 — сегодня), другие города — СДЭК ~3 дня */
const MON_R = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
function whenLabel(){
  const f = load("amura-form", {}), city = f.city || (AUTH.profile && AUTH.profile.city) || "";
  const now = new Date(Date.now() + (new Date().getTimezoneOffset() + 300) * 60000);    // время Алматы (UTC+5)
  if(!city || /алмат/i.test(city)) return now.getHours() < 13 ? "Сегодня" : "Завтра";
  const d = new Date(now.getTime() + 3 * 864e5); return d.getDate() + " " + MON_R[d.getMonth()];
}
const buyLabel = q => q ? `В корзине · ${q}` : whenLabel();
cardHTML = function(it){
  const q = cart[it.id] || 0;
  return `<article class="card rpc" data-id="${esc(it.id)}">
    <div class="rpc-img">${favBtnHTML(it.id)}
      <button class="thumb" data-open aria-label="${esc(it.name)}">${it.img ? `<img src="${esc(it.img)}" alt="" loading="lazy" decoding="async" width="500" height="500">` : `<span class="ph">${esc((it.brand || it.name || "A").charAt(0))}</span>`}${it.isNew ? '<span class="tag">Новинка</span>' : ""}</button>
      <button type="button" class="rpc-add${q ? " in" : ""}" data-radd aria-label="${q ? `В корзине ${q} шт, добавить ещё` : "В корзину"}">${CART_SVG}${q ? `<em>${q}</em>` : ""}</button>
    </div>
    <div class="rpc-price">${fmt(unitPrice(it, 1))} ₸</div>
    <button type="button" class="rpc-name" data-open>${VERIFIED}${it.brand && it.brand.length <= 20 && !it.name.toLowerCase().startsWith(it.brand.toLowerCase()) ? `<b>${esc(it.brand)}</b> / ` : ""}${esc(it.name)}</button>
    ${rateHTML(it.id)}
    ${it.qty <= 5 ? `<div class="rpc-low">Осталось ${fmt(it.qty)} шт</div>` : ""}
    <button type="button" class="rpc-buy${q ? " in" : ""}" data-radd>${CART_SVG}<span>${esc(buyLabel(q))}</span></button>
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
  document.querySelectorAll(`.rpc[data-id="${CSS.escape(id)}"] .rpc-buy`).forEach(b => {
    b.classList.toggle("in", !!q); b.querySelector("span").textContent = buyLabel(q);
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
