/* Розничный сайт (amura.kz/shop): корзина и оформление заказа как на WB — два шага.
   Шаг 1 «Корзина»: адрес сверху, товары с галочками, срок доставки у каждого, «Купить» один товар, кнопка «К оформлению» внизу.
   Шаг 2 «Оформление заказа»: всё, что ниже.
   Только для розницы — make_shop.py вставляет этот файл в shop/index.html; оптовый index.html не меняется.
   Способ получения вкладками (пункт выдачи / курьер / самовывоз), адрес — строкой со стрелкой (выбор в окне),
   срок и цена доставки сразу, товары, получатель, способ оплаты, итог справа (компьютер) или снизу (телефон).
   Заказать можно только после входа или регистрации; покупатель помечается в МойСклад меткой «розница». */
(function(){
if(!window.AMURA_RETAIL) return;
const MON = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
const WANT = "amura-r-checkout";                 // «после входа вернуться к оформлению»
/* способы получения: Алматы — Яндекс «в течение дня», пункт СДЭК, самовывоз, Express; другие города — только пункт выдачи СДЭК */
const isAlm = f => DLV.opts && DLV.city === ((f && f.city) || "") ? DLV.opts.almaty : /алмат/i.test((f && f.city) || "");
const tabsFor = f => isAlm(f) ? [["courier", "В течение дня"], ["cdek", "Пункт СДЭК"], ["pickup", "Самовывоз"], ["express", "Express"]] : [["cdek", "Пункт выдачи СДЭК"]];
const isCour = s => s === "courier" || s === "express";
const _dlvPrice = dlvPrice;
dlvPrice = function(ship, goods){          // цена по способу: своя цена и свой порог бесплатной доставки (rates с сервера)
  if(!ship || ship === "pickup") return 0;
  const o = DLV.opts; if(!o) return null;
  const r = (o.rates || {})[ship]; if(!r) return _dlvPrice(ship, goods);
  return r.free && goods >= r.free ? 0 : r.price;
};
const freeFrom = ship => { const r = DLV.opts && (DLV.opts.rates || {})[ship]; return r && r.free || 0; };
const plural = (n, a, b, c) => { const m = n % 10, h = n % 100; return m === 1 && h !== 11 ? a : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? b : c; };
const PIN = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-6.5-6.2-6.5-11.2a6.5 6.5 0 0 1 13 0C18.5 14.8 12 21 12 21Z"/><circle cx="12" cy="9.8" r="2.3"/></svg>';
const CHEV = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 6 6 6-6 6"/></svg>';
const TRASH = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 7h15M9.5 7V5h5v2M6.5 7l1 12.5h9l1-12.5"/></svg>';
const CARD = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M3 9.5h18M7 14.5h4"/></svg>';

/* выбранные галочкой товары: в заказ идут только они (снятые — остаются в корзине) */
const OFF = "amura-r-off";
let off = new Set(load(OFF, []));
const saveOff = () => save(OFF, [...off]);
function tot(ship){
  const ls = lines().filter(l => !off.has(l.it.id));
  const goods = ls.reduce((s, l) => s + l.price * l.q, 0), dlv = ls.length ? dlvPrice(ship, goods) : 0;
  return { ls, goods, dlv, total: goods + (dlv || 0), count: ls.reduce((s, l) => s + l.q, 0) };
}
let STEP = "cart", NEXT = "";          // cart | checkout
function form(){
  const f = load("amura-form", {}), pr = AUTH.profile || {};
  f.name = f.name || pr.name || ""; f.city = f.city || pr.city || "";
  const ids = tabsFor(f).map(t => t[0]); if(!ids.includes(f.ship)) f.ship = ids[0];
  f.phone = f.phone || (pr.phone ? "+" + pr.phone : "");
  return f;
}
function saveForm(patch){ const f = { ...form(), ...patch }; f.pvz = DLV.pvzSel || null; f.slot = DLV.slotSel || null; save("amura-form", f); return f; }

function dayLabel(n){
  if(n <= 0) return "Сегодня"; if(n === 1) return "Завтра"; if(n === 2) return "Послезавтра";
  const d = new Date(Date.now() + n * 864e5); return d.getDate() + " " + MON[d.getMonth()];
}
function rangeLabel(a, b){
  if(!b || a === b || b <= 2) return dayLabel(b || a);
  const da = new Date(Date.now() + a * 864e5), db = new Date(Date.now() + b * 864e5);
  return da.getMonth() === db.getMonth() ? `${da.getDate()}–${db.getDate()} ${MON[db.getMonth()]}` : `${dayLabel(a)} – ${dayLabel(b)}`;
}
function slotDay(s){ const d = Math.round((new Date(s.date + "T00:00:00") - new Date(new Date().toDateString())) / 864e5); return dayLabel(d); }
function method(id){ return DLV.opts && (DLV.opts.methods || []).find(m => m.id === id); }
/* срок: СДЭК — по калькулятору (+1 день, пока передаём посылку); курьер по Алматы — выбранный или ближайший интервал */
function eta(f){
  const o = DLV.opts;
  if(f.ship === "pickup") return "Самовывоз";
  if(!o) return "";
  if(f.ship === "express") return "Сегодня, за 1–2 часа";
  if(f.ship === "courier" && o.almaty){ const s = DLV.slotSel || (o.slots || [])[0]; return s ? `${slotDay(s)}, ${s.from}–${s.to}` : "В течение дня"; }
  const m = method(f.ship); if(!m || !m.days) return f.ship === "cdek" ? "Пункт выдачи" : "Курьер";
  return rangeLabel(m.days[0] + 1, m.days[1] + 1);
}
function whenTxt(f){
  if(f.ship === "pickup") return "Самовывоз — бесплатно";
  if(!f.city || !DLV.opts) return f.city ? "Считаем срок доставки…" : "Укажите адрес — покажем срок доставки";
  const e = eta(f);
  return /^\d/.test(e) ? "Доставим " + e : /^(Сегодня|Завтра|Послезавтра)/.test(e) ? "Доставим " + e.charAt(0).toLowerCase() + e.slice(1) : e;
}
const priceTxt = p => p === null ? "—" : p ? fmt(p) + " ₸" : "бесплатно";

function addrRow(f){
  const city = f.city || "";
  if(f.ship === "pickup"){
    const a = (DLV.opts && method("pickup") && method("pickup").note) || "Со склада в Алматы";
    return `<div class="raddr static">${PIN}<span><b>${esc(a)}</b><small>${esc((DLV.opts && DLV.opts.hours) || "Заберёте сами — когда заказ будет готов, сообщим")}</small></span></div>`;
  }
  let title, sub;
  if(!city){ title = "Укажите город"; sub = "Посчитаем цену и срок доставки"; }
  else if(f.ship === "cdek"){ const p = DLV.pvzSel; title = p ? p.address : (f.address || "Выберите пункт выдачи"); sub = city + (p ? " · " + p.type : " · пункты и постаматы СДЭК"); }
  else { title = f.address || "Укажите адрес доставки"; sub = city; }
  return `<button type="button" class="raddr" data-raddr>${PIN}<span><b class="${/^(Укажите|Выберите)/.test(title) ? "ph" : ""}">${esc(title)}</b><small>${esc(sub)}</small></span>${CHEV}</button>`;
}
function slotsHTML(f){
  const o = DLV.opts;
  if(f.ship !== "courier" || !o || !o.almaty || !(o.slots || []).length) return "";
  return `<div class="rslots-h">Когда доставить</div><div class="rslots">${o.slots.slice(0, 9).map(s => {
    const on = DLV.slotSel && DLV.slotSel.date === s.date && DLV.slotSel.from === s.from;
    return `<button type="button" class="${on ? "on" : ""}" data-rslot="${esc(s.date + "|" + s.from)}"><b>${esc(slotDay(s))}</b><small>${esc(s.from)}–${esc(s.to)}</small></button>`; }).join("")}</div>`;
}
/* какая служба везёт: курьер по Алматы — Яндекс Доставка, в другие города и пункты — СДЭК */
function provHTML(f){
  const o = DLV.opts, alm = o ? o.almaty : /алмат/i.test(f.city || "");
  const [name, note] = f.ship === "pickup" ? ["AMURA", "заберёте сами со склада, бесплатно"]
    : f.ship === "express" ? ["Яндекс Экспресс", "курьер едет сразу после сборки, за 1–2 часа"]
    : f.ship === "courier" ? ["Яндекс Доставка", "привезём за 4 часа в выбранное окно"]
    : ["СДЭК", alm ? "пункт выдачи или постамат" : "пункт выдачи или постамат · в другие города только так"];
  return `<div class="rprov"><b class="${name === "Яндекс Доставка" ? "ya" : name === "СДЭК" ? "cd" : ""}">${name}</b><span>${note}</span></div>`;
}
function dlvCard(f, t){
  const TB = tabsFor(f);
  const tabs = TB.map(([id, name]) => {
    const p = id === "pickup" ? 0 : dlvPrice(id, t.goods);
    return `<button type="button" role="tab" aria-selected="${f.ship === id}" data-rm="${id}">${name}${p === null ? "" : `<small>${priceTxt(p)}</small>`}</button>`; }).join("");
  const e = eta(f), w = t.dlv === null ? (f.city ? "считаем…" : "") : priceTxt(t.dlv);
  return `<div class="rtabs n${TB.length}" role="tablist">${tabs}</div>${provHTML(f)}${addrRow(f)}
    <div class="reta"><b>${esc(e)}${w ? `, <span class="${t.dlv === 0 ? "free" : ""}">${w}</span>` : ""}</b><span>${fmt(t.count)} шт</span></div>
    ${DLV.opts && DLV.opts.warn ? `<div class="kv">${esc(DLV.opts.warn)}</div>` : ""}
    <div class="rthumbs">${t.ls.slice(0, 8).map(l => `<span>${l.it.img ? `<img src="${esc(l.it.img)}" alt="" loading="lazy">` : `<i>${esc((l.it.brand || l.it.name || "A").charAt(0))}</i>`}${l.q > 1 ? `<em>${l.q}</em>` : ""}</span>`).join("")}${t.ls.length > 8 ? `<span><i>+${t.ls.length - 8}</i></span>` : ""}</div>
    ${slotsHTML(f)}`;
}
function totalsHTML(f, t){
  const o = DLV.opts, ff = freeFrom(f.ship), rest = ff && t.goods < ff ? ff - t.goods : 0, restC = 0;
  const login = !AUTH.token;
  return `<div class="rtot"><span>Итого</span><b>${fmt(t.total)} ₸</b></div>
    <div class="rrow"><span>${fmt(t.count)} ${plural(t.count, "товар", "товара", "товаров")} на сумму</span><span>${fmt(t.goods)} ₸</span></div>
    <div class="rrow"><span>Доставка</span><span class="${t.dlv === 0 ? "free" : ""}">${t.dlv === null ? (f.city ? "считаем…" : "укажите город") : priceTxt(t.dlv)}</span></div>
    ${rest ? `<div class="rfree">До бесплатной доставки ещё <b>${fmt(rest)} ₸</b><i><b style="width:${Math.round(100 * t.goods / ff)}%"></b></i></div>` : ""}
    <div class="err" id="rErr"></div>
    <button type="button" class="btn rgo" id="rGo">${login ? "Войти и заказать" : "Заказать"}</button>
    ${window.AMURA_LEGAL ? `<div class="rterms">Нажимая «Заказать», вы принимаете условия <a href="legal/offer.html" target="_blank">оферты</a> и <a href="legal/privacy.html" target="_blank">политики конфиденциальности</a></div>` : ""}
    ${login ? `<div class="rhint mut">Чтобы оформить заказ, войдите или зарегистрируйтесь — по номеру телефона или через Telegram, без пароля</div>` : ""}`;
}

const plTov = n => `${fmt(n)} ${plural(n, "товар", "товара", "товаров")}`;
function renderRetailCart(){
  const title = $("#drawerTitle"), body = $("#drawerBody");
  body.parentElement.classList.add("rwide");
  const all = lines();
  if(!all.length){
    STEP = "cart"; title.textContent = "Корзина"; body.parentElement.classList.remove("rstep2");
    body.innerHTML = `<div class="notice" style="border:0"><h3>Корзина пуста</h3><p>Добавьте товары из каталога — корзина сохранится, даже если закрыть страницу.</p></div>`;
    return;
  }
  [...off].forEach(id => { if(!cart[id]) off.delete(id); });
  const f = form();
  if(STEP === "checkout" && !tot(f.ship).ls.length) STEP = "cart";
  STEP === "checkout" ? stepCheckout(f) : stepCart(f, all);
  body.parentElement.classList.toggle("rstep2", STEP === "checkout");
  placeFix();
  if(f.city && (!DLV.opts || DLV.city !== f.city)) loadDlv(f.city);
}
/* ---------- шаг 1: корзина ---------- */
function stepCart(f, all){
  const t = tot(f.ship), allOn = all.every(l => !off.has(l.it.id)), w = whenTxt(f);
  $("#drawerTitle").innerHTML = `Корзина<small class="rsub">${plTov(all.length)}</small>`;
  $("#drawerBody").innerHTML = `<div class="rco rcart">
    <div class="rmain">
      <div class="rcard rtopaddr" id="rAddrTop">${addrRow(f)}</div>
      <div class="rcard rbar"><label class="rchk"><input type="checkbox" id="rAll" ${allOn ? "checked" : ""}><span>Все</span></label>
        <button type="button" class="rico" id="rDelSel" aria-label="Удалить выбранные" ${t.ls.length ? "" : "disabled"}>${TRASH}</button></div>
      ${all.map(l => { const on = !off.has(l.it.id);
        return `<div class="rcard ritem${on ? "" : " roff"}" data-rid="${esc(l.it.id)}">
        <label class="rpic">${l.it.img ? `<img src="${esc(l.it.img)}" alt="" loading="lazy">` : `<i>${esc((l.it.brand || l.it.name || "A").charAt(0))}</i>`}
          <input type="checkbox" data-rsel aria-label="Выбрать" ${on ? "checked" : ""}></label>
        <div class="rinfo">
          <div class="rprice"><b>${fmt(l.price * l.q)} ₸</b>${l.q > 1 ? `<small>${fmt(l.price)} ₸ / шт</small>` : ""}</div>
          <div class="rname">${esc(l.it.name)}</div>
          <div class="rwhen">${esc(w)}</div>
          <div class="ract"><div class="line rstep" data-id="${esc(l.it.id)}"><div class="stepper"><button data-dec aria-label="Меньше">−</button><input type="number" inputmode="numeric" min="0" max="${l.it.qty}" value="${l.q}" aria-label="Количество"><button data-inc aria-label="Больше">+</button></div></div>
            <button type="button" class="rbuy" data-rbuy>Купить</button></div>
        </div>
        <button type="button" class="rico rdel" data-rdel aria-label="Удалить">${TRASH}</button></div>`; }).join("")}
    </div>
    <aside class="rcard rside" id="rTot">${cartTotalsHTML(f, t)}</aside></div>
    <div class="rfix" id="rFix"><button type="button" class="rfixb" data-rnext ${t.ls.length ? "" : "disabled"}><span>К оформлению · ${fmt(t.count)}</span><b>${fmt(t.total)} ₸</b></button></div>`;
}
function cartTotalsHTML(f, t){
  return `<div class="rtot"><span>Итого</span><b>${fmt(t.total)} ₸</b></div>
    <div class="rrow"><span>${plTov(t.count)} на сумму</span><span>${fmt(t.goods)} ₸</span></div>
    <div class="rrow"><span>Доставка</span><span class="${t.dlv === 0 ? "free" : ""}">${t.dlv === null ? (f.city ? "считаем…" : "укажите адрес") : priceTxt(t.dlv)}</span></div>
    <button type="button" class="btn rgo" data-rnext ${t.ls.length ? "" : "disabled"}>Перейти к оформлению</button>
    ${t.ls.length ? "" : `<div class="rhint mut">Отметьте товары, которые хотите заказать</div>`}`;
}
/* ---------- шаг 2: оформление ---------- */
function stepCheckout(f){
  const t = tot(f.ship), pr = AUTH.profile || {};
  $("#drawerTitle").innerHTML = `<button type="button" class="rback" data-rback>← Корзина</button>Оформление заказа<small class="rsub" id="rSub">${plTov(t.count)}, ${fmt(t.total)} ₸</small>`;
  $("#drawerBody").innerHTML = `<div class="rco">
    <div class="rmain">
      <section class="rcard" id="rDlv">${dlvCard(f, t)}</section>
      <section class="rcard"><h3>Получатель</h3>
        ${AUTH.token ? `<div class="rrcp">
          <div class="field"><label for="rName">Имя</label><input id="rName" autocomplete="name" value="${esc(f.name)}"></div>
          <div class="field"><label for="rPhone">Телефон</label><input id="rPhone" type="tel" inputmode="tel" autocomplete="tel" placeholder="+7 7__ ___ __ __" value="${esc(f.phone)}"></div></div>
          ${pr.telegram && !pr.phone ? `<div class="kv">Номер нужен курьеру и СДЭК для связи с вами.</div>` : ""}`
        : `<div class="rlogin"><p>Войдите или зарегистрируйтесь, чтобы оформить заказ. Это быстро — по номеру телефона или через Telegram, без пароля.</p>
          <button type="button" class="btn ghost" data-rlogin>Войти или зарегистрироваться</button></div>`}</section>
      <section class="rcard"><h3>Способ оплаты</h3>
        <div class="rpay">${CARD}<span><b>Оплата после подтверждения</b><small>Менеджер проверит наличие и пришлёт ссылку на оплату. Оплата картой на сайте — скоро.</small></span></div></section>
    </div>
    <aside class="rcard rside" id="rTot">${totalsHTML(f, t)}</aside></div>
    <div class="rfix" id="rFix"><button type="button" class="rfixb" data-rgo><span>${AUTH.token ? "Заказать" : "Войти и заказать"}</span><b>${fmt(t.total)} ₸</b></button></div>`;
}
function paint(){
  if(VIEW !== "cart" || !$("#rFix")) return;
  const f = form();
  if(STEP === "cart"){
    const t = tot(f.ship), w = whenTxt(f);
    $("#rAddrTop").innerHTML = addrRow(f);
    document.querySelectorAll(".rwhen").forEach(x => x.textContent = w);
    $("#rTot").innerHTML = cartTotalsHTML(f, t);
    const b = $("#rFix .rfixb"); b.disabled = !t.ls.length; b.innerHTML = `<span>К оформлению · ${fmt(t.count)}</span><b>${fmt(t.total)} ₸</b>`;
  } else {
    const t = tot(f.ship);
    $("#rDlv").innerHTML = dlvCard(f, t);
    $("#rTot").innerHTML = totalsHTML(f, t);
    $("#rFix .rfixb b").textContent = fmt(t.total) + " ₸";
    const s = $("#rSub"); if(s) s.textContent = `${plTov(t.count)}, ${fmt(t.total)} ₸`;
  }
  placeFix();
}
/* кнопка внизу (телефон) — над нижней панелью сайта */
function placeFix(){
  const fx = $("#rFix"); if(!fx) return;
  const tb = document.querySelector(".tabbar"), r = tb && getComputedStyle(tb).display !== "none" ? tb.getBoundingClientRect() : null;
  fx.style.bottom = (r && r.height ? Math.max(8, innerHeight - r.top + 8) : 16) + "px";
}
addEventListener("resize", () => placeFix());
function toCheckout(){
  if(!tot(form().ship).ls.length) return toast("Отметьте товары, которые хотите заказать");
  if(!AUTH.token){ NEXT = "checkout"; return toLogin(); }
  STEP = "checkout"; renderRetailCart(); scrollTo({ top: 0 });
}

/* ---------- окно выбора адреса: город, пункт выдачи или адрес курьера ---------- */
function openSheet(){
  const f = form();
  let el = $("#rSheet");
  if(!el){ el = document.createElement("div"); el.id = "rSheet"; el.className = "rsheet"; document.body.appendChild(el);
    el.addEventListener("click", sheetClick); el.addEventListener("input", sheetInput); el.addEventListener("change", sheetChange); }
  const cdek = f.ship === "cdek";
  el.innerHTML = `<div class="rsbox" role="dialog" aria-modal="true" aria-label="${cdek ? "Пункт выдачи" : "Адрес доставки"}">
    <div class="rsh"><b>${cdek ? "Пункт выдачи СДЭК" : "Адрес доставки"}</b><button type="button" class="rx" data-rclose aria-label="Закрыть">×</button></div>
    <div class="field"><label for="rCity">Город</label><input id="rCity" autocomplete="address-level2" placeholder="Например, Алматы" value="${esc(f.city)}"></div>
    <div id="rSheetBody"></div>
    <div class="err" id="rsErr"></div>
    <button type="button" class="btn" id="rsOk">${cdek ? "Выбрать" : "Готово"}</button></div>`;
  el.hidden = false; document.body.classList.add("rsheet-open");
  sheetBody();
  if(!f.city) setTimeout(() => $("#rCity").focus(), 50);
}
function sheetBody(){
  const f = form(), b = $("#rSheetBody"); if(!b) return;
  const hd = $("#rSheet .rsh b"), ok = $("#rsOk");
  if(hd) hd.textContent = f.ship === "cdek" ? "Пункт выдачи СДЭК" : "Адрес доставки"; if(ok) ok.textContent = f.ship === "cdek" ? "Выбрать" : "Готово";
  if(f.ship === "cdek"){
    b.innerHTML = f.city ? `<input id="pvzQ" placeholder="Поиск по адресу" autocomplete="off"><div class="pvzl" id="pvzList"><div class="kv">Загружаем пункты…</div></div>` : `<div class="kv">Укажите город — покажем пункты и постаматы СДЭК</div>`;
    if(f.city) loadPvz(f.city);
  } else {
    const o = DLV.opts;
    b.innerHTML = `<div class="field"><label for="rAddr">Улица, дом, квартира</label><input id="rAddr" autocomplete="street-address" placeholder="Например, Абая 10, кв 5" value="${esc(f.address || "")}"></div>
      <div class="kv" id="rsNote">${courierNote()}</div>`;
  }
}
function courierNote(){
  const f = form(), o = DLV.opts;
  return f.city && o ? `${f.ship === "express" ? "Срочный курьер Яндекс, за 1–2 часа" : "Курьер Яндекс привезёт в выбранное окно"} · ${priceTxt(dlvPrice(f.ship, tot(f.ship).goods))}` : "";
}
function closeSheet(){ const el = $("#rSheet"); if(el) el.hidden = true; document.body.classList.remove("rsheet-open"); paint(); }
let cityT = 0;
function sheetInput(e){
  if(e.target.id === "rCity"){ clearTimeout(cityT); cityT = setTimeout(() => {
    const city = e.target.value.trim(); if(city === form().city) return;
    DLV.pvzSel = null; DLV.pvz = []; DLV.pvzFor = ""; saveForm({ city, address: "" }); Promise.resolve(loadDlv(city)).then(() => { saveForm({}); sheetBody(); }); sheetBody(); }, 600); }
  if(e.target.id === "pvzQ") drawPvz(e.target.value);
}
function sheetChange(e){
  if(e.target.name === "pvz"){ DLV.pvzSel = DLV.pvz.find(p => String(p.code) === e.target.value) || null;
    document.querySelectorAll("#pvzList label").forEach(l => l.classList.toggle("on", l.querySelector("input").checked)); saveForm({}); }
}
function sheetClick(e){
  if(e.target.id === "rSheet" || e.target.closest("[data-rclose]")) return closeSheet();
  if(e.target.id !== "rsOk") return;
  const f = form(), city = $("#rCity").value.trim(), err = $("#rsErr");
  if(!city){ err.textContent = "Укажите город"; return $("#rCity").focus(); }
  if(city !== f.city){ DLV.pvzSel = null; saveForm({ city }); loadDlv(city); sheetBody(); return; }
  if(f.ship === "cdek"){
    const manual = $("#fAddr") ? $("#fAddr").value.trim() : "";
    if(!DLV.pvzSel && !manual){ err.textContent = "Выберите пункт выдачи"; return; }
    saveForm({ address: manual });
  } else {
    const a = $("#rAddr").value.trim();
    if(!a){ err.textContent = "Укажите улицу, дом и квартиру"; return $("#rAddr").focus(); }
    saveForm({ address: a });
  }
  closeSheet();
}

/* ---------- корзина: вкладки, интервалы, получатель, заказ ---------- */
$("#drawerBody").addEventListener("change", e => {
  if(!$("#rFix")) return;
  if(e.target.id === "rAll"){ if(e.target.checked) off.clear(); else lines().forEach(l => off.add(l.it.id)); saveOff(); renderRetailCart(); }
  const it = e.target.closest("[data-rsel]") && e.target.closest(".ritem");
  if(it){ e.target.checked ? off.delete(it.dataset.rid) : off.add(it.dataset.rid); saveOff(); renderRetailCart(); }
});
$("#drawerBody").addEventListener("click", e => {
  if(!$("#rFix") && !e.target.closest("[data-rlogin]")) return;
  if(e.target.closest("[data-rnext]")) return toCheckout();
  if(e.target.closest("[data-rback]")){ STEP = "cart"; renderRetailCart(); return; }
  if(e.target.closest("[data-rgo]") || e.target.id === "rGo") return submit();
  const item = e.target.closest(".ritem");
  if(item && e.target.closest("[data-rbuy]")){ lines().forEach(l => l.it.id === item.dataset.rid ? off.delete(l.it.id) : off.add(l.it.id)); saveOff(); return toCheckout(); }
  if(item && e.target.closest("[data-rdel]")){ const it = byId(item.dataset.rid); if(it) setCartQty(it, 0); return; }
  if(e.target.closest("#rDelSel")){
    const sel = tot(form().ship).ls; if(!sel.length || !confirm(`Удалить из корзины: ${plTov(sel.length)}?`)) return;
    sel.forEach(l => delete cart[l.it.id]); save("amura-cart2", cart); renderCartCount(); render(); renderRetailCart(); return;
  }
  const tab = e.target.closest("[data-rm]");
  if(tab){ saveForm({ ship: tab.dataset.rm }); paint(); const f = form(); if(f.ship !== "pickup" && (!f.city || (f.ship === "cdek" && !DLV.pvzSel && !f.address) || (isCour(f.ship) && !f.address))) openSheet(); return; }
  if(e.target.closest("[data-raddr]")) return openSheet();
  const sl = e.target.closest("[data-rslot]");
  if(sl && DLV.opts){ const [date, from] = sl.dataset.rslot.split("|"); DLV.slotSel = DLV.opts.slots.find(s => s.date === date && s.from === from) || null; saveForm({}); paint(); return; }
  if(e.target.closest("[data-rlogin]")){ NEXT = "checkout"; return toLogin(); }
});
$("#drawerBody").addEventListener("input", e => {
  if(e.target.id === "rName") saveForm({ name: e.target.value.trim() });
  if(e.target.id === "rPhone") saveForm({ phone: e.target.value.trim() });
});
// «← Корзина» стоит в заголовке, а не в #drawerBody — свой обработчик
$("#drawerTitle").addEventListener("click", e => { if(e.target.closest("[data-rback]") && $("#rFix")){ STEP = "cart"; renderRetailCart(); scrollTo({ top: 0 }); } });
function toLogin(){ try{ sessionStorage.setItem(WANT, "1"); }catch{} go("me"); }

/* после заказа — как на WB: «Заказ оформлен», состав, доставка, что дальше; без WhatsApp и PDF — заказ уже у менеджера */
function doneRetail(res, f, t){
  const total = res.total || t.total, ph = String(f.phone || "").replace(/\D/g, "");
  const phone = ph.length === 11 ? `+${ph[0]} ${ph.slice(1, 4)} ${ph.slice(4, 7)} ${ph.slice(7, 9)} ${ph.slice(9)}` : (f.phone || "");
  const where = f.ship === "pickup" ? ((method("pickup") && method("pickup").note) || "Склад в Алматы")
    : f.ship === "cdek" ? (f.pvz ? f.pvz.address : f.address) : f.address;
  const how = { courier: "Курьер Яндекс, в течение дня", express: "Express, курьер Яндекс за 1–2 часа", cdek: "Пункт выдачи СДЭК", pickup: "Самовывоз" }[f.ship] || "";
  const when = f.ship === "courier" && f.slot ? `${slotDay(f.slot)}, ${f.slot.from}–${f.slot.to}` : f.ship === "express" ? "Сегодня, за 1–2 часа после подтверждения" : eta(f);
  $("#drawerTitle").textContent = "Заказ оформлен";
  $("#drawerBody").innerHTML = `<div class="done rdone">
    <div class="rdone-h">${CHECK_BIG}<div><h3>Спасибо! Заказ № ${esc(res.number)}</h3><p>${plTov(t.count)} на ${fmt(total)} ₸</p></div></div>
    <ol class="rsteps">
      <li class="on"><b>Заказ принят</b><span>Товары зарезервированы за вами</span></li>
      <li><b>Подтвердим и пришлём ссылку на оплату</b><span>Напишем в WhatsApp на ${esc(phone)} — обычно в течение 15 минут в рабочее время</span></li>
      <li><b>${f.ship === "pickup" ? "Можно забирать" : "Доставка"}</b><span>${esc(f.ship === "pickup" ? "Сообщим, когда заказ будет собран" : when || "")}</span></li>
    </ol>
    <div class="rcard rdone-d">
      <div class="rrow"><span>Получение</span><b>${esc(how)}</b></div>
      ${where ? `<div class="rrow"><span>${f.ship === "pickup" ? "Адрес склада" : "Адрес"}</span><b>${esc(where)}</b></div>` : ""}
      ${f.ship === "courier" && when ? `<div class="rrow"><span>Когда</span><b>${esc(when)}</b></div>` : ""}
      <div class="rrow"><span>Получатель</span><b>${esc(f.name || "")}, ${esc(phone)}</b></div>
      <div class="rthumbs">${t.ls.slice(0, 8).map(l => `<span>${l.it.img ? `<img src="${esc(l.it.img)}" alt="" loading="lazy">` : `<i>${esc((l.it.brand || l.it.name || "A").charAt(0))}</i>`}${l.q > 1 ? `<em>${l.q}</em>` : ""}</span>`).join("")}</div>
      <div class="rrow"><span>Товары</span><span>${fmt(t.goods)} ₸</span></div>
      <div class="rrow"><span>Доставка</span><span>${t.dlv ? fmt(t.dlv) + " ₸" : "бесплатно"}</span></div>
      <div class="rtot"><span>Итого</span><b>${fmt(total)} ₸</b></div>
    </div>
    <button class="btn" id="rdOrders" type="button">Мои заказы</button>
    <button class="btn ghost" id="rdShop" type="button">Продолжить покупки</button>
  </div>`;
  $("#rdOrders").onclick = () => go("me");
  $("#rdShop").onclick = () => go("home");
  scrollTo({ top: 0 });
}
const CHECK_BIG = '<svg class="rdone-ok" viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="22"/><path d="M14 24.5l7 7 13-14"/></svg>';

let busy = false;
async function submit(){
  if(busy) return;
  const err = $("#rErr"), f = form(), t = tot(f.ship);
  const fail = (m, sel) => { err.textContent = m; toast(m); if(sel && $(sel)){ $(sel).focus(); $(sel).scrollIntoView({ block: "center", behavior: "smooth" }); } };
  if(!AUTH.token){ NEXT = "checkout"; return toLogin(); }
  if(!t.ls.length){ STEP = "cart"; return renderRetailCart(); }
  const digits = normPhone(String(f.phone).replace(/\D/g, ""));
  if(!f.name) return fail("Укажите имя получателя", "#rName");
  if(!/^7\d{10}$/.test(digits)) return fail("Укажите телефон получателя", "#rPhone");
  if(f.ship !== "pickup" && !f.city) return openSheet();
  if(f.ship === "cdek" && !DLV.pvzSel && !f.address){ fail("Выберите пункт выдачи"); return openSheet(); }
  if(isCour(f.ship) && !f.address){ fail("Укажите адрес доставки"); return openSheet(); }
  if(f.ship === "courier" && DLV.opts && DLV.opts.almaty && (DLV.opts.slots || []).length && !DLV.slotSel) return fail("Выберите, когда доставить", "[data-rslot]");
  if(t.dlv === null && f.ship !== "pickup") return fail("Считаем доставку — секунду…");
  err.textContent = "";
  const payload = {
    name: f.name, contact: "wa", phone: digits, telegram: "",
    city: f.city || (AUTH.profile && AUTH.profile.city) || "Алматы", shipping: f.ship, address: f.address || "",
    items: t.ls.map(l => ({ id: l.it.id, qty: l.q, price: l.price })), clientTotal: t.total,
    orderKey: orderKey(), mode: "retail",
    pvz: f.ship === "cdek" ? DLV.pvzSel : undefined, slot: f.ship === "courier" ? DLV.slotSel : undefined
  };
  const btns = [...document.querySelectorAll("#rGo, [data-rgo]")]; busy = true;
  btns.forEach(b => { b.disabled = true; b.dataset.t = b.innerHTML; b.textContent = "Отправляем…"; });
  try{
    let res;
    if(DEMO){ await new Promise(r => setTimeout(r, 700)); res = { ok: true, number: "DEMO-" + Date.now().toString().slice(-5), total: t.total, pdfUrl: "", demo: true }; }
    else{
      const ctl = new AbortController(), tm = setTimeout(() => ctl.abort(), 120000);
      let r;
      try{ r = await fetch(CONFIG.orderUrl, { method: "POST", headers: authHeaders(), body: JSON.stringify(payload), signal: ctl.signal }); }
      catch(e){ throw new Error(e.name === "AbortError" ? "Сервер долго отвечает — нажмите «Заказать» ещё раз, дубль не создастся" : "Нет связи с сервером"); }
      finally{ clearTimeout(tm); }
      res = await r.json().catch(() => ({}));
      if(r.status === 401 && res.login){ setAuth("", null); toast("Войдите заново"); return toLogin(); }
      if(!r.ok || !res.ok) throw new Error(res.error || "Сервер не ответил");
    }
    $("#drawerBody").parentElement.classList.remove("rwide", "rstep2");
    doneRetail(res, { ...f, pvz: DLV.pvzSel, slot: DLV.slotSel, address: f.address || "" }, t);
    orderKeyVal = ""; DLV.slotSel = null; STEP = "cart";
    t.ls.forEach(l => delete cart[l.it.id]); save("amura-cart2", cart); renderCartCount(); render();   // заказанное убираем, остальное остаётся
  }catch(ex){ err.textContent = (ex.message || "Ошибка") + ". Попробуйте ещё раз."; toast(ex.message || "Ошибка"); }
  finally{ busy = false; btns.forEach(b => { if(b.isConnected){ b.disabled = false; b.innerHTML = b.dataset.t; } }); }
}

/* после входа — обратно к оформлению; вошедший на рознице помечается «розница» (один раз на вход) */
function markRetail(){
  if(!AUTH.token || DEMO) return;
  const k = "amura-r-marked"; let done = ""; try{ done = localStorage.getItem(k) || ""; }catch{}
  if(done === AUTH.token.slice(-16)) return;
  api("/me/retail", { method: "POST" }).then(() => { try{ localStorage.setItem(k, AUTH.token.slice(-16)); }catch{} }).catch(() => {});
}
const _paintMe = paintMe;
paintMe = function(p, orders){
  _paintMe(p, orders); markRetail();
  let want = false; try{ want = sessionStorage.getItem(WANT) === "1"; }catch{}
  if(want && AUTH.token && p && p.name){ try{ sessionStorage.removeItem(WANT); }catch{} toast("Вы вошли — оформляем заказ"); NEXT = "checkout"; go("cart"); }
};

renderCart = renderRetailCart;
const _shipName = dlvShipName;
dlvShipName = function(f){ return f.ship === "express" ? "Срочный курьер по Алматы (Яндекс Экспресс) — " + (f.address || "") : _shipName(f); };
/* розница: вход и регистрация только по номеру телефона (код по SMS), без Telegram */
prepareTg = function(){};
const _renderAuth = renderAuth;
renderAuth = function(){
  _renderAuth();
  ["#tgLogin", "#tgHint", "#meBody .or"].forEach(sel => { const el = $(sel); if(el) el.remove(); });
  const send = $("#aSend");
  if(send && !$("#aRemember")){
    send.insertAdjacentHTML("beforebegin", `<label class="rremember"><input type="checkbox" id="aRemember" ${remember() ? "checked" : ""}><span>Запомнить меня<small>Снимите на чужом телефоне — вход сбросится, когда закроете браузер</small></span></label>`);
    $("#aRemember").onchange = e => { try{ localStorage.setItem(REM, e.target.checked ? "1" : "0"); }catch{} };
  }
};
/* «Запомнить меня»: по умолчанию вход хранится на устройстве (180 дней); без галочки — только до закрытия браузера */
const REM = "amura-r-remember";
function remember(){ try{ return localStorage.getItem(REM) !== "0"; }catch{ return true; } }
const _save = save;
save = function(k, v){
  if(k !== "amura-auth" || remember()) return _save(k, v);
  try{ sessionStorage.setItem(k, JSON.stringify(v)); localStorage.removeItem(k); }catch{}
};
if(!AUTH.token && !remember()){
  try{ const v = JSON.parse(sessionStorage.getItem("amura-auth") || "null"); if(v && v.token){ AUTH = v; if(VIEW === "me") renderMe(); } }catch{}
}
const _api = api;
api = async function(path, opts){
  try{ return await _api(path, opts); }
  catch(e){ if(path === "/auth/sms/send" && /telegram/i.test(e.message)) throw new Error("Вход по SMS временно недоступен. Попробуйте позже или напишите нам в WhatsApp"); throw e; }
};
const _go = go;
go = function(v, keep){ if(v === "cart"){ STEP = NEXT || "cart"; NEXT = ""; } return _go(v, keep); };
dlvRefresh = () => { paint(); const n = $("#rsNote"); if(n) n.textContent = courierNote(); };
document.addEventListener("keydown", e => { if(e.key === "Escape" && $("#rSheet") && !$("#rSheet").hidden) closeSheet(); });
markRetail();
if(VIEW === "cart") renderCart();
if(VIEW === "me" && !AUTH.token) renderAuth();
})();
