/* Розничный сайт: «Подбор ухода» (анкета → рутина утро / вечер / раз в неделю из товаров в наличии, «Всё в корзину», «Эконом»)
   и на странице товара — «Состав простым языком», «С чем сочетать» и «Похожие дешевле». Работает, только когда в системе AMURA
   включён модуль «Подбор ухода (ИИ)» (/care/status). Товары и цены выбирает сервер по каталогу (care.py), ИИ их не выдумывает.
   Анкета без имени и телефона, ответы не сохраняются. Только розница. */
(function(){
if(!window.AMURA_RETAIL) return;
const API = () => CONFIG.apiUrl;
let ON = false;
const INFO = {};

/* ---------- вопросы ---------- */
const Q = [
  { k: "skin", t: "Какая у вас кожа?", s: "Если не уверены — выберите «Не знаю», подскажем", o: [
    ["dry", "Сухая", "Стянутость, бывает шелушение"], ["oily", "Жирная", "Блестит почти везде, заметные поры"],
    ["combo", "Комбинированная", "Блестит лоб, нос и подбородок"], ["normal", "Нормальная", "Комфортно, без проблем"], ["unknown", "Не знаю", "Пройти мини-тест"]] },
  { k: "test", t: "Мини-тест: как кожа ощущается через 2–3 часа после умывания, без крема?", if: a => a.skin === "unknown", o: [
    ["dry", "Стянута, хочется нанести крем"], ["oily", "Блестит везде"], ["combo", "Блестит только лоб, нос, подбородок"], ["normal", "Комфортно, без блеска и стянутости"]] },
  { k: "sens", t: "Кожа часто краснеет или щиплет от новых средств?", o: [["yes", "Да, часто"], ["some", "Иногда"], ["no", "Нет"], ["skip", "Не указывать"]] },
  { k: "concerns", t: "Что хочется улучшить?", s: "До трёх вариантов", multi: 3, o: [
    ["acne", "Высыпания"], ["pores", "Поры, чёрные точки"], ["pigment", "Пятна и неровный тон"], ["dry", "Сухость, шелушение"],
    ["red", "Покраснения"], ["aging", "Морщины, упругость"], ["dull", "Тусклый цвет"], ["none", "Ничего особенного — просто ухаживать"], ["skip", "Не указывать"]] },
  { k: "age", t: "Ваш возраст", o: [["18", "До 25"], ["25", "25–34"], ["35", "35–44"], ["45", "45 и старше"]] },
  { k: "have", t: "Что у вас уже есть и вы хотите оставить?", s: "Эти шаги не положим в корзину. Можно ничего не выбирать", multi: 6, opt: true, o: [
    ["cleanser", "Средство для умывания"], ["toner", "Тонер"], ["serum", "Сыворотка"], ["cream", "Крем"], ["spf", "Солнцезащитный крем"]] },
  { k: "level", t: "Сколько шагов готовы делать?", o: [
    ["min", "Минимум", "Умывание, крем, SPF"], ["std", "Стандарт", "+ тонер и сыворотка"], ["full", "Полный уход", "+ снятие макияжа, крем для глаз, пилинг и маски"]] },
  { k: "budget", t: "Бюджет на весь уход", o: [["low", "До 15 000 ₸"], ["mid", "15 000 – 30 000 ₸"], ["high", "Больше 30 000 ₸"]] },
];
const S = { i: 0, a: {}, res: null, tab: "main", off: new Set() };
const steps = () => Q.filter(q => !q.if || q.if(S.a));

function view(){
  let v = $("#rCare");
  if(!v){
    v = document.createElement("div"); v.id = "rCare"; v.className = "rcare"; v.setAttribute("role", "dialog"); v.setAttribute("aria-modal", "true"); v.setAttribute("aria-label", "Подбор ухода");
    document.body.appendChild(v); v.addEventListener("click", onClick);
  }
  return v;
}
function open(){
  if(!ON) return;
  S.i = 0; S.a = {}; S.res = null; S.tab = "main"; S.off = new Set();
  document.body.classList.add("rcare-on"); paint();
  if(location.hash !== "#care") try{ history.pushState({ care: 1 }, "", "#care"); }catch{}
}
function close(back){
  const v = $("#rCare"); if(v) v.remove();
  document.body.classList.remove("rcare-on");
  if(back !== false && location.hash === "#care") try{ history.back(); }catch{}
}
addEventListener("popstate", () => { if($("#rCare") && location.hash !== "#care") close(false); });

function head(title, back){
  return `<div class="rcare-bar"><button type="button" class="rp-back" data-cback aria-label="Назад"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg></button>
    <b>${esc(title)}</b><button type="button" class="rx" data-cclose aria-label="Закрыть">×</button></div>`;
}
function paint(){
  const v = view();
  if(S.res) return paintRes(v);
  const st = steps(), q = st[S.i], cur = S.a[q.k];
  const sel = id => q.multi ? (cur || []).includes(id) : cur === id;
  v.innerHTML = `${head("Подбор ухода")}<div class="rcare-in">
    <div class="rcare-prog" aria-hidden="true"><i style="width:${Math.round(100 * (S.i + 1) / st.length)}%"></i></div>
    <div class="rcare-n">Вопрос ${S.i + 1} из ${st.length}</div>
    <h2 class="rcare-q">${esc(q.t)}</h2>${q.s ? `<p class="rcare-s">${esc(q.s)}</p>` : ""}
    <div class="rcare-opts" role="${q.multi ? "group" : "radiogroup"}">${q.o.map(([id, t, d]) =>
      `<button type="button" class="rcare-o${sel(id) ? " on" : ""}" role="${q.multi ? "checkbox" : "radio"}" aria-checked="${sel(id)}" data-copt="${id}"><b>${esc(t)}</b>${d ? `<small>${esc(d)}</small>` : ""}</button>`).join("")}</div>
    ${q.multi ? `<button type="button" class="btn rcare-next" data-cnext ${!q.opt && !(cur || []).length ? "disabled" : ""}>${S.i === st.length - 1 ? "Подобрать уход" : "Дальше"}</button>` : ""}
    <p class="rcare-fine">Без имени и телефона, ответы не сохраняем. Подбор — по товарам в наличии на сайте.</p></div>`;
  v.scrollTop = 0;
}
function choose(id){
  const q = steps()[S.i];
  if(q.multi){
    let cur = [...(S.a[q.k] || [])];
    if(id === "none" || id === "skip") cur = cur.includes(id) ? [] : [id];
    else{ cur = cur.filter(x => x !== "none" && x !== "skip"); cur = cur.includes(id) ? cur.filter(x => x !== id) : cur.concat(id); if(cur.length > q.multi) cur.shift(); }
    S.a[q.k] = cur; return paint();
  }
  S.a[q.k] = id; next();
}
function next(){
  const st = steps();
  if(S.i < st.length - 1){ S.i++; return paint(); }
  submit();
}
async function submit(){
  const v = view(), a = S.a;
  v.innerHTML = `${head("Подбор ухода")}<div class="rcare-in rcare-wait"><div class="rkwait"><span class="rkdot"></span>Подбираем уход из товаров в наличии…</div></div>`;
  const body = { skin: a.skin === "unknown" ? a.test : a.skin, sens: a.sens, concerns: (a.concerns || []).filter(x => x !== "none" && x !== "skip"),
    age: a.age, have: a.have || [], level: a.level, budget: a.budget };
  try{
    const r = await fetch(API() + "/care/quiz", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if(!r.ok || !j.ok) throw new Error(j.error || "Не получилось подобрать уход, попробуйте ещё раз");
    S.res = j; S.tab = "main"; S.off = new Set(); paint();
  }catch(e){ toast(e.message); S.i = steps().length - 1; paint(); }
}

/* ---------- результат ---------- */
const cur = () => S.res[S.tab === "eco" ? "eco" : "main"];
const inCart = s => !s.have && !s.again && !S.off.has(s.id);
function picked(){
  const r = cur(), m = new Map();
  ["am", "pm", "week"].forEach(k => r[k].forEach(s => { if(inCart(s) && !m.has(s.id)) m.set(s.id, s); }));
  return [...m.values()];
}
function stepHTML(s){
  const it = byId(s.id), img = s.img || (it && it.img);
  const chk = !s.have && !s.again;
  return `<div class="rcare-st${s.have ? " have" : ""}">
    <a class="rcare-im" href="#p/${encodeURIComponent(s.id)}" data-cp="${esc(s.id)}">${img ? `<img src="${esc(img)}" alt="" loading="lazy">` : `<i>${esc((s.brand || "A").charAt(0))}</i>`}</a>
    <div class="rcare-tx"><small>${esc(s.title)}</small><a href="#p/${encodeURIComponent(s.id)}" data-cp="${esc(s.id)}" class="rcare-nm">${esc(s.name)}</a>
      <span class="rcare-fr">${esc(s.freq)}</span>${s.why ? `<span class="rcare-why">${esc(s.why)}</span>` : ""}
      ${s.have ? `<span class="rcare-hv">У вас уже есть — можно оставить своё</span>` : s.again ? `<span class="rcare-hv">То же средство, что утром</span>` : ""}</div>
    <div class="rcare-pr"><b>${fmt(s.price)} ₸</b>${chk ? `<label class="rcare-ck"><input type="checkbox" data-cck="${esc(s.id)}" ${S.off.has(s.id) ? "" : "checked"} aria-label="В корзину"></label>` : ""}</div></div>`;
}
function paintRes(v){
  const j = S.res, r = cur(), p = picked(), sum = p.reduce((t, s) => t + s.price, 0);
  const sec = (k, t) => r[k].length ? `<section class="rcard rcare-sec"><h3>${t}</h3>${r[k].map(stepHTML).join("")}</section>` : "";
  v.innerHTML = `${head("Ваш уход")}<div class="rcare-in rcare-res">
    <div class="rcare-sum"><b>${esc(j.skin[0].toUpperCase() + j.skin.slice(1))} кожа${j.concerns.length ? " · " + esc(j.concerns.join(", ").toLowerCase()) : ""}</b><p>${esc(j.summary || "")}</p></div>
    <div class="rtabs n2" role="tablist"><button type="button" role="tab" aria-selected="${S.tab === "main"}" data-ctab="main">Основной<small>${fmt(j.main.total)} ₸</small></button>
      <button type="button" role="tab" aria-selected="${S.tab === "eco"}" data-ctab="eco">Эконом<small>${fmt(j.eco.total)} ₸</small></button></div>
    ${sec("am", "Утро")}${sec("pm", "Вечер")}${sec("week", "1–2 раза в неделю")}
    ${r.missing.length ? `<div class="rcare-miss">Сейчас нет в наличии подходящего: ${esc(r.missing.join(", ").toLowerCase())}</div>` : ""}
    ${(j.notes || []).length ? `<ul class="rcare-notes">${j.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    <p class="rcare-disc">${esc(j.disclaimer)}</p>
    <button type="button" class="btn ghost" data-cagain>Пройти заново</button></div>
    <div class="rcare-fix"><button type="button" class="btn" data-ccart ${p.length ? "" : "disabled"}>Всё в корзину · ${fmt(p.length)} ${p.length === 1 ? "товар" : p.length < 5 ? "товара" : "товаров"} · ${fmt(sum)} ₸</button></div>`;
}
function toCart(){
  let n = 0;
  picked().forEach(s => { const it = byId(s.id); if(it && (cart[s.id] || 0) < (it.qty || 1)){ cart[s.id] = (cart[s.id] || 0) + 1; n++; } });
  save("amura-cart2", cart); renderCartCount();
  toast(n ? `Добавлено в корзину: ${n}` : "Эти товары уже в корзине");
  if(n){ close(); go("cart"); }
}
function onClick(e){
  const t = e.target;
  if(t.closest("[data-cclose]")) return close();
  if(t.closest("[data-cback]")){ if(S.res){ S.res = null; S.i = steps().length - 1; return paint(); } if(S.i > 0){ S.i--; return paint(); } return close(); }
  const o = t.closest("[data-copt]"); if(o) return choose(o.dataset.copt);
  if(t.closest("[data-cnext]")) return next();
  const tb = t.closest("[data-ctab]"); if(tb){ S.tab = tb.dataset.ctab; S.off = new Set(); return paint(); }
  const ck = t.closest("[data-cck]"); if(ck){ ck.checked ? S.off.delete(ck.dataset.cck) : S.off.add(ck.dataset.cck); const y = $("#rCare").scrollTop; paint(); $("#rCare").scrollTop = y; return; }
  if(t.closest("[data-ccart]")) return toCart();
  if(t.closest("[data-cagain]")){ S.res = null; S.i = 0; S.a = {}; return paint(); }
  const pp = t.closest("[data-cp]"); if(pp){ e.preventDefault(); const it = byId(pp.dataset.cp); if(it){ close(false); openProduct(it); } }
}

/* ---------- вход: карточка на главной и ссылка #care ---------- */
function entry(){
  if(!ON || $("#rCareCta")) return;
  const hero = document.querySelector("#viewHome .hero .wrap"); if(!hero) return;
  hero.insertAdjacentHTML("beforeend", `<button type="button" class="rcare-cta" id="rCareCta" data-care>
    <span class="rcare-ci" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3c2.5 3.2 6 5.6 6 10a6 6 0 0 1-12 0c0-4.4 3.5-6.8 6-10Z"/><path d="M9.5 14.5c.6 1.3 1.6 2 3 2"/></svg></span>
    <span><b>Подобрать уход</b><small>Около минуты — утро и вечер из товаров в наличии</small></span><svg class="rcare-ca" viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 6 6 6-6 6"/></svg></button>`);
}
document.addEventListener("click", e => { if(e.target.closest("[data-care]")){ e.preventDefault(); open(); } });

/* ---------- страница товара: состав простым языком, сочетания, похожие дешевле ---------- */
function infoHTML(j){
  const acts = (j.actives || []).map(a => `<li><b>${esc(a.name)}</b> — ${esc(a.what)}</li>`).join("");
  const li = (arr, cls) => arr.map(x => `<li class="${cls}">${esc(x)}</li>`).join("");
  if(!j.simple && !acts && !(j.combine || []).length && !(j.avoid || []).length) return "";
  return `<section class="rcare-p">
    ${j.simple || acts ? `<h3>Состав простым языком</h3>${j.simple ? `<p>${esc(j.simple)}</p>` : ""}${acts ? `<ul class="rcare-acts">${acts}</ul>` : ""}
      ${j.suits ? `<p class="rcare-fit"><b>Кому подойдёт:</b> ${esc(j.suits)}</p>` : ""}${j.caution ? `<p class="rcare-fit"><b>С осторожностью:</b> ${esc(j.caution)}</p>` : ""}` : ""}
    ${(j.combine || []).length || (j.avoid || []).length ? `<h3>С чем сочетать</h3><ul class="rcare-pair">${li(j.combine || [], "ok")}${li(j.avoid || [], "no")}</ul>` : ""}
    ${j.freq ? `<p class="rcare-fit"><b>Как часто:</b> ${esc(j.freq)}</p>` : ""}
    <p class="rcare-disc">${esc(j.disclaimer || "")}</p></section>`;
}
async function onProduct(it){
  if(!ON || !it) return;
  let j = INFO[it.id];
  if(!j){ try{ j = INFO[it.id] = await getJSON(`${API()}/care/info/${encodeURIComponent(it.id)}`, 15000); }catch{ return; } }
  const sh = $("#sheet"); if(!j || !j.found || !sh || sh.dataset.id !== it.id || sh.querySelector(".rcare-p, .rcare-sim")) return;
  const html = infoHTML(j), desc = sh.querySelector("#descBox");
  if(html && desc) desc.insertAdjacentHTML("afterend", html);
  const sim = (j.similar || []).map(byId).filter(Boolean);
  if(sim.length){
    const sec = document.createElement("section"); sec.className = "rrec-sec insheet rcare-sim";
    sec.innerHTML = `<h2 class="rrec-h">Похожие дешевле</h2><div class="grid rrec">${sim.map(cardHTML).join("")}</div>`;
    const first = sh.querySelector(".rrec-sec"); first ? first.before(sec) : sh.appendChild(sec);
  }
}
const _open = openProduct;
openProduct = function(it){ const r = _open.apply(this, arguments); onProduct(it); return r; };

/* ---------- включено ли ---------- */
getJSON(`${API()}/care/status`, 15000).then(j => {
  ON = !!(j && j.on);
  if(!ON) return;
  const tryEntry = () => { entry(); if(!$("#rCareCta")) setTimeout(tryEntry, 500); }; tryEntry();
  if(location.hash === "#care"){ try{ history.replaceState(null, "", location.pathname); }catch{} open(); }
  const sh = $("#sheet"); if(sh && document.body.classList.contains("rpage") && sh.dataset.id) onProduct(byId(sh.dataset.id));
}).catch(() => {});
})();
