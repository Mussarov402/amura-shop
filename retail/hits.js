/* Розничный сайт: главная как на WB — под баннерами «Подобрали для вас» одной лентой с бесконечной подгрузкой:
   1) хиты из системы AMURA (закреплённые и продажи за 30 дней — считает сервер);
   2) личная подборка — бренды и группы товаров, которые посетитель смотрел, добавлял в избранное или в корзину
      (история только на устройстве, на сервер не уходит); купленное и то, что уже в корзине, не повторяем;
   3) после ~20 карточек — «Популярное сейчас»: остальные товары по популярности.
   Внутри блоков лёгкое перемешивание — своё у каждого посетителя, но одно и то же в течение сессии.
   В поиске и в бренде — обычный список с сортировкой («Популярные» по умолчанию). Метка «Хит» на карточке. Только розница.
   Та же подборка (window.recsFor) — в «Вам может понравиться» на остальных экранах (retail/recs.js). */
(function(){
if(!window.AMURA_RETAIL) return;
let HITS = [], POP = new Map();
const HSET = () => new Set(HITS);
const FIRST = 20;                                  // столько карточек в «Подобрали для вас», дальше «Популярное сейчас»

const _apply = applyData;   // в живом каталоге сервер присылает hits и pop; в статичном их нет — оставляем прежние
applyData = function(data){
  if(data && Array.isArray(data.pop)){ HITS = data.hits || []; POP = new Map(data.pop.map((id, i) => [id, i])); }
  return _apply.apply(this, arguments);
};

/* история на устройстве: просмотренные товары и купленные (из «Моих заказов») */
const SEEN = "amura-seen", BOUGHT = "amura-bought";
const _open = openProduct;
openProduct = function(it){
  if(it && it.id){ const s = load(SEEN, []).filter(x => x !== it.id); s.unshift(it.id); save(SEEN, s.slice(0, 40)); }
  return _open.apply(this, arguments);
};
const _me = paintMe;
paintMe = function(p, orders){
  if(Array.isArray(orders)) save(BOUGHT, [...new Set(orders.flatMap(o => (o.items || []).map(i => i.id)))].slice(0, 300));
  return _me.apply(this, arguments);
};

/* перемешивание: своё зерно на сессию — порядок стабилен, пока вкладка открыта */
const SEED = (() => { try{ let v = sessionStorage.getItem("amura-seed"); if(!v){ v = String(Math.floor(Math.random() * 1e9)); sessionStorage.setItem("amura-seed", v); } return +v; }catch{ return 7; } })();
const noise = id => { let h = SEED; for(let i = 0; i < id.length; i++) h = Math.imul(h ^ id.charCodeAt(i), 2654435761) >>> 0; return (h % 1000) / 1000; };
const popRank = it => POP.has(it.id) ? POP.get(it.id) : POP.size + 1;

function taste(){                                  // вес бренда и группы: смотрел — 1, избранное и корзина — 3
  const w = { b: new Map(), g: new Map() }, add = (id, k) => { const it = byId(id); if(!it) return;
    if(it.brand) w.b.set(it.brand, (w.b.get(it.brand) || 0) + k);
    if(it.group) w.g.set(it.group, (w.g.get(it.group) || 0) + k); };
  load(SEEN, []).forEach((id, i) => add(id, i < 10 ? 1 : 0.5));
  FAV.forEach(id => add(id, 3));
  Object.keys(cart).forEach(id => add(id, 3));
  return w;
}
let FEED = null;                                   // id первого товара «Популярного сейчас»
function rank(list){                               // [подборка: хиты + личное, остальное по популярности]
  const hs = HSET(), skip = new Set([...Object.keys(cart), ...load(BOUGHT, [])]), w = taste();
  const hits = HITS.map(byId).filter(it => it && list.includes(it)).sort((a, b) => (HITS.indexOf(a.id) + 3 * noise(a.id)) - (HITS.indexOf(b.id) + 3 * noise(b.id)));
  const score = it => (w.b.get(it.brand) || 0) + (w.g.get(it.group) || 0);
  const mine = list.filter(it => !hs.has(it.id) && !skip.has(it.id) && score(it) > 0)
    .sort((a, b) => score(b) * (0.8 + 0.4 * noise(b.id)) - score(a) * (0.8 + 0.4 * noise(a.id)) || popRank(a) - popRank(b));
  const top = [...hits, ...mine].slice(0, FIRST), used = new Set(top.map(it => it.id));
  const rest = list.filter(it => !used.has(it.id)).sort((a, b) => popRank(a) * (0.85 + 0.3 * noise(a.id)) - popRank(b) * (0.85 + 0.3 * noise(b.id)));
  if(top.length < FIRST){ const n = FIRST - top.length; top.push(...rest.splice(0, n)); }   // новый посетитель: хиты, затем популярные
  return [top, rest];
}
function forYou(list){
  const [top, rest] = rank(list);
  FEED = rest.length ? rest[0].id : null;
  return [...top, ...rest];
}
/* «Вам может понравиться» в карточке товара, избранном, корзине, кабинете и после заказа (retail/recs.js) — та же подборка без того, что уже на экране */
window.recsFor = exclude => { const ex = new Set(exclude || []); return rank(ALL.filter(it => !ex.has(it.id))).flat(); };
window.popRank = popRank;

const home = () => !st.q && !st.brand && st.sort === "pop";   // главная — подборка; поиск, бренд и баннер «Новинки» — обычный список с сортировкой
const sel = $("#sort");
if(sel && !sel.querySelector('option[value="pop"]')){ sel.insertAdjacentHTML("afterbegin", '<option value="pop">Популярные</option>'); }
if(st.sort === "default"){ st.sort = "pop"; if(sel) sel.value = "pop"; }
$("#home").addEventListener("click", () => { st.sort = "pop"; if(sel) sel.value = "pop"; }, true);   // логотип — снова подборка
let FY = null;                                    // порядок подборки до следующей перерисовки — подгрузка при прокрутке его не меняет
const _filtered = filtered;
filtered = function(){
  const list = _filtered.apply(this, arguments);
  if(home()) return FY || (FY = forYou(list));
  FEED = null;
  if(st.sort === "pop"){ const n = POP.size; list.sort((a, b) => (POP.has(a.id) ? POP.get(a.id) : n) - (POP.has(b.id) ? POP.get(b.id) : n) || (a.brand || "").localeCompare(b.brand || "", "ru") || a.name.localeCompare(b.name, "ru")); }
  return list;
};
const _render = render;
render = function(){
  FY = null;
  const r = _render.apply(this, arguments);
  if(home()) $("#title").textContent = "Подобрали для вас";
  if(sel) sel.style.display = home() ? "none" : "";
  mark();
  return r;
};
/* заголовок «Популярное сейчас» в ленте — и после подгрузки следующих карточек */
const grid = $("#grid");
function mark(){
  const h = grid.querySelector(".rfy-h"), at = FEED && home() ? grid.querySelector(`.card[data-id="${CSS.escape(FEED)}"]`) : null;
  if(!at){ if(h && !FEED) h.remove(); return; }
  if(h && h.nextElementSibling === at) return;
  if(h) h.remove();
  at.insertAdjacentHTML("beforebegin", '<h2 class="rfy-h">Популярное сейчас</h2>');
}
new MutationObserver(() => mark()).observe(grid, { childList: true });

/* метка «Хит» на карточке (CSS), без изменения разметки карточки */
const _card = cardHTML;
cardHTML = function(it){ const h = _card.apply(this, arguments); return HSET().has(it.id) ? h.replace('class="thumb" data-open', 'class="thumb" data-open data-hit') : h; };
})();
