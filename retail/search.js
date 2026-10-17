/* Розничный сайт: поиск как на WB. Нажали в строку поиска — экран поиска:
   пустое поле — «Вы искали» (до 10 последних запросов, только в браузере: amura-q), «Часто ищут» (бренды популярных товаров),
   «Вы смотрели» (история amura-seen); печатают (от 2 букв) — подсказки: бренды и товары с фото и ценой, «Искать «…»».
   Подсказки — по уже загруженному каталогу, без запросов к серверу. Список товаров — только по «Найти» (без поиска на каждую букву).
   Точный код/артикул/штрихкод по «Найти» сразу открывает товар (общий код в opt/index.html). Только розница. */
(function(){
if(!window.AMURA_RETAIL) return;
const QK = "amura-q", MAXH = 10;
const q = $("#q"), form = $("#searchForm");
const hist = () => load(QK, []);
const remember = v => { v = String(v || "").trim(); if(v.length < 2) return; save(QK, [v, ...hist().filter(x => x.toLowerCase() !== v.toLowerCase())].slice(0, MAXH)); };

const panel = document.createElement("section");
panel.className = "rsrch"; panel.hidden = true; panel.setAttribute("aria-label", "Поиск");
document.body.appendChild(panel);
form.insertAdjacentHTML("afterend", '<button type="button" class="rs-cancel" id="rsCancel">Отменить</button>');

function place(){                                  // под шапкой; на компьютере — выпадающим окном под строкой поиска
  const hb = document.querySelector("header.top").getBoundingClientRect(), fb = form.getBoundingClientRect();
  panel.style.setProperty("--rs-top", Math.max(0, hb.bottom) + "px");
  panel.style.setProperty("--rs-left", fb.left + "px"); panel.style.setProperty("--rs-width", fb.width + "px");
}
const bold = (t, w) => { const i = t.toLowerCase().indexOf(w.toLowerCase()); return i < 0 ? esc(t) : esc(t.slice(0, i)) + "<b>" + esc(t.slice(i, i + w.length)) + "</b>" + esc(t.slice(i + w.length)); };
function popular(){
  const rank = window.popRank, cnt = new Map();
  ALL.slice().sort((a, b) => rank ? rank(a) - rank(b) : 0).slice(0, 150).forEach(i => { if(i.brand) cnt.set(i.brand, (cnt.get(i.brand) || 0) + 1); });
  return [...cnt.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(x => x[0]);
}
function drawEmpty(){
  const H = hist(), P = popular(), seen = [...new Set(load("amura-seen", []))].map(byId).filter(Boolean).slice(0, 12);
  panel.innerHTML = (H.length ? `<div class="rs-h"><b>Вы искали</b><button type="button" class="rs-clr" data-rs-clear>Очистить</button></div>
      <div class="rs-chips">${H.map((x, i) => `<span class="rs-chip"><button type="button" data-rs-q="${esc(x)}">${esc(x)}</button><button type="button" class="rs-x" data-rs-del="${i}" aria-label="Удалить «${esc(x)}»">✕</button></span>`).join("")}</div>` : "")
    + (P.length ? `<div class="rs-h"><b>Часто ищут</b></div><div class="rs-chips">${P.map(b => `<span class="rs-chip"><button type="button" data-rs-brand="${esc(b)}">${esc(b)}</button></span>`).join("")}</div>` : "")
    + (seen.length ? `<div class="rs-h"><b>Вы смотрели</b></div><div class="grid rs-seen">${seen.map(cardHTML).join("")}</div>` : "")
    || '<p class="kv rs-none">Введите название, бренд или артикул</p>';
}
function drawSuggest(w){
  const lw = w.toLowerCase();
  const brands = [...new Set(ALL.map(i => i.brand).filter(Boolean))].filter(b => b.toLowerCase().includes(lw))
    .sort((a, b) => b.toLowerCase().startsWith(lw) - a.toLowerCase().startsWith(lw) || a.localeCompare(b, "ru")).slice(0, 3);
  const its = ALL.filter(i => (i.name + " " + (i.brand || "") + " " + (i.code || "") + " " + (i.article || "")).toLowerCase().includes(lw))
    .sort((a, b) => (window.popRank ? popRank(a) - popRank(b) : 0)).slice(0, 8 - brands.length);
  panel.innerHTML = `<div class="rs-list">
    ${brands.map(b => `<button type="button" class="rs-row" data-rs-brand="${esc(b)}"><span class="rs-ic" aria-hidden="true">🏷️</span><span class="rs-t">${bold(b, w)}<small>бренд</small></span></button>`).join("")}
    ${its.map(i => `<button type="button" class="rs-row" data-rs-item="${esc(i.id)}"><span class="rs-ph">${i.img ? `<img src="${esc(i.img)}" alt="" loading="lazy">` : ""}</span><span class="rs-t">${bold(i.name, w)}<small>${fmt(i.opt)} ₸</small></span></button>`).join("")}
    <button type="button" class="rs-row rs-all" data-rs-q="${esc(w)}"><span class="rs-ic" aria-hidden="true">🔍</span><span class="rs-t">Искать «${esc(w)}»</span></button></div>`;
}
let dt = 0;
function draw(){ const w = q.value.trim(); if(w.length >= 2) drawSuggest(w); else drawEmpty(); }
function open(){ if(!panel.hidden) return; place(); draw(); panel.hidden = false; document.documentElement.classList.add("rs-open"); }
function close(){ panel.hidden = true; document.documentElement.classList.remove("rs-open"); }
function search(v){ q.value = v; remember(v); close(); form.requestSubmit(); }

q.addEventListener("focus", open);
document.addEventListener("input", e => {                // пока печатают — подсказки, а не список товаров (общий живой поиск отключаем)
  if(e.target !== q) return;
  e.stopImmediatePropagation(); if(window.qSync) qSync();
  open(); clearTimeout(dt); dt = setTimeout(draw, 150);
}, true);
form.addEventListener("submit", () => { remember(q.value); close(); }, true);
$("#rsCancel").onclick = () => { close(); q.value = st.q || ""; if(window.qSync) qSync(); q.blur(); };
$("#qx").addEventListener("click", () => setTimeout(() => { open(); draw(); }, 0));
document.addEventListener("click", e => {
  if(panel.hidden) return;
  if(!panel.contains(e.target) && !form.contains(e.target) && e.target.id !== "rsCancel"){ close(); return; }
  const t = e.target;
  const qb = t.closest("[data-rs-q]"); if(qb) return search(qb.dataset.rsQ);
  const del = t.closest("[data-rs-del]"); if(del){ const H = hist(); H.splice(+del.dataset.rsDel, 1); save(QK, H); q.focus(); return drawEmpty(); }
  if(t.closest("[data-rs-clear]")){ save(QK, []); q.focus(); return drawEmpty(); }
  const br = t.closest("[data-rs-brand]"); if(br){ close(); q.value = ""; st.q = ""; if(window.qSync) qSync(); q.blur(); return selectBrand(br.dataset.rsBrand); }
  const it = t.closest("[data-rs-item]"); if(it){ const x = byId(it.dataset.rsItem); remember(q.value); close(); q.blur(); if(x) openProduct(x); return; }
  const card = t.closest(".rs-seen [data-id]");
  if(card && !t.closest("[data-radd]")){ if(t.closest("[data-open]")) close(); onBuyClick(e); }
});
addEventListener("resize", () => { if(!panel.hidden) place(); });
document.addEventListener("keydown", e => { if(e.key === "Escape" && !panel.hidden){ close(); q.blur(); } });
})();
