/* Розничный сайт (amura.kz): корзина и оформление заказа как на WB — два шага.
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
/* Express — только в часы приёма (настраиваются в панели): сервер не отдаёт его в способах, вкладку не показываем */
const xOff = f => DLV.opts && DLV.city === ((f && f.city) || "") && DLV.opts.express && !DLV.opts.express.open;
const tabsFor = f => isAlm(f) ? [["courier", "В течение дня"], ["cdek", "Пункт СДЭК"], ["pickup", "Самовывоз"], ["express", "Express"]].filter(t => t[0] !== "express" || !xOff(f)) : [["cdek", "Пункт выдачи СДЭК"]];
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
const KASPI = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="2.5" width="12" height="19" rx="3"/><path d="M10.5 18.5h3M9.5 9.5l2 2 3.5-4"/></svg>';

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
    ${xOff(f) ? `<div class="kv">Express — ${esc(DLV.opts.express.hours || "")}${DLV.opts.express.next ? `. Откроется ${esc(DLV.opts.express.next)}` : ""}</div>` : ""}
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
    ${PROMO_ON || PROMO ? `<div class="field" style="margin-top:10px"><label for="fPromo">Промокод</label><div style="display:flex;gap:8px"><input id="fPromo" autocapitalize="characters" autocomplete="off" placeholder="Если есть" value="${esc(PROMO ? PROMO.code : "")}" style="flex:1;min-width:0"><button class="btn ghost" id="promoBtn" type="button" style="width:auto;flex:none;padding:0 14px">${PROMO ? "Убрать" : "Применить"}</button></div><div class="kv" id="promoMsg" style="margin-top:4px">${PROMO ? (PROMO.discount > 0 ? `Скидка ${PROMO.discount}% учтена в цене` : "Промокод применён") : ""}</div></div>` : ""}
    <div class="err" id="rErr"></div>
    ${consentHTML("rAgree", CONSENT.cart)}
    ${LEGAL() ? `<label class="rremember ragree"><input type="checkbox" id="rNews" ${CONSENT.news ? "checked" : ""}><span>Хочу получать новости и акции AMURA (SMS, e-mail, мессенджеры)</span></label>` : ""}
    <button type="button" class="btn rgo" id="rGo" ${LEGAL() && !CONSENT.cart ? 'aria-disabled="true"' : ""}>${goTxt(t, true)}</button>
    ${login ? `<div class="rhint mut">Чтобы оформить заказ, войдите или зарегистрируйтесь — по номеру телефона или через Telegram, без пароля</div>` : ""}`;
}

/* согласие с офертой и политикой (требование юриста): галочка не отмечена по умолчанию, без неё «Заказать» и «Получить код» неактивны.
   Только на amura.kz (window.AMURA_LEGAL); на опте ничего этого нет */
const LEGAL = () => !!window.AMURA_LEGAL;
/* оплата: «Картой онлайн» (Freedom Pay) и/или «Kaspi» (счёт в приложение Kaspi через ApiPay) — заказ появится после оплаты.
   Оба выключены — как раньше (менеджер пришлёт ссылку). Включены оба — покупатель выбирает, по умолчанию карта */
const payNow = () => !!(window.CARD_PAY || window.KASPI_PAY);
const PAYM = { v: "" };
const payWay = () => window.CARD_PAY && window.KASPI_PAY ? (PAYM.v || "card") : window.KASPI_PAY ? "kaspi" : window.CARD_PAY ? "card" : "";
const goTxt = (t, sum) => !AUTH.token ? "Войти и заказать" : payNow() ? "Заказать и оплатить" + (sum ? ` ${fmt(t.total)} ₸` : "") : "Заказать";
function payHTML(){
  if(!payNow()) return `<div class="rpay">${CARD}<span><b>Оплата после подтверждения</b><small>Менеджер проверит наличие и пришлёт ссылку на оплату. Оплата картой на сайте — скоро.</small></span></div>`;
  const card = `${CARD}<span><b>Картой онлайн</b><small>Visa, Mastercard, Apple Pay, Google Pay — сразу после «Заказать и оплатить», на защищённой странице Freedom Pay</small></span>`;
  const kaspi = `${KASPI}<span><b>Kaspi</b><small>Счёт придёт в приложение Kaspi на номер получателя — откройте и оплатите</small></span>`;
  if(!window.KASPI_PAY) return `<div class="rpay">${card}</div>`;
  if(!window.CARD_PAY) return `<div class="rpay">${kaspi}</div>`;
  const w = payWay();
  return `<div class="rpays" role="radiogroup" aria-label="Способ оплаты">
    <button type="button" class="rpay rpm${w === "card" ? " on" : ""}" role="radio" aria-checked="${w === "card"}" data-rpaym="card">${card}</button>
    <button type="button" class="rpay rpm${w === "kaspi" ? " on" : ""}" role="radio" aria-checked="${w === "kaspi"}" data-rpaym="kaspi">${kaspi}</button></div>`;
}
const CONSENT = { cart: false, auth: false, news: false };
function consentHTML(id, on){
  if(!LEGAL()) return "";
  return `<label class="rremember ragree"><input type="checkbox" id="${id}" ${on ? "checked" : ""}><span>Я принимаю условия <a href="/shop/legal/offer.html" target="_blank" rel="noopener">оферты</a> и даю согласие на обработку персональных данных согласно <a href="/shop/legal/privacy.html" target="_blank" rel="noopener">Политике конфиденциальности</a>
    <small class="ragree-h" id="${id}H" ${on ? "hidden" : ""}>Отметьте галочку, чтобы продолжить</small></span></label>`;
}
function consentNeed(id){   // нажали неактивную кнопку — подсвечиваем подсказку и галочку
  const h = $("#" + id + "H"), box = $("#" + id);
  if(h){ h.hidden = false; h.classList.add("bad"); }
  if(box){ box.scrollIntoView({ block: "center", behavior: "smooth" }); box.focus(); }
  toast("Отметьте согласие с офертой и политикой конфиденциальности");
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
        ${payHTML()}</section>
    </div>
    <aside class="rcard rside" id="rTot">${totalsHTML(f, t)}</aside></div>
    <div class="rfix" id="rFix"><button type="button" class="rfixb" data-rgo ${LEGAL() && !CONSENT.cart ? 'aria-disabled="true"' : ""}><span>${goTxt(t)}</span><b>${fmt(t.total)} ₸</b></button></div>`;
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
    <div class="field rcity"><label for="rCity">Город</label><input id="rCity" autocomplete="off" placeholder="Начните вводить: Алматы, Астана…" value="${esc(f.city)}"><div class="rcl" id="rCityList" hidden></div></div>
    <div id="rSheetBody"></div>
    <div class="err" id="rsErr"></div>
    <button type="button" class="btn" id="rsOk">${cdek ? "Выбрать" : "Готово"}</button></div>`;
  el.hidden = false; document.body.classList.add("rsheet-open");
  sheetBody();
  $("#rCity").addEventListener("focus", e => drawCities(e.target.value === form().city ? "" : e.target.value));
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
/* города Казахстана для подсказки: сначала крупные, потом все остальные по алфавиту (можно вписать и свой посёлок) */
const KZ_TOP = ["Алматы","Астана","Шымкент","Караганда","Актобе","Тараз","Павлодар","Усть-Каменогорск","Семей","Атырау","Костанай","Кызылорда",
  "Уральск","Петропавловск","Актау","Туркестан","Кокшетау","Талдыкорган","Экибастуз","Конаев"];
const KZ_CITIES = KZ_TOP.concat(["Абай","Акколь","Аксай","Аксу","Алга","Алтай","Аральск","Аркалык","Арыс","Атбасар","Аягоз","Байконур","Балхаш",
  "Булаево","Державинск","Ерейментау","Есик","Есиль","Жанаозен","Жанатас","Жаркент","Жезказган","Жем","Жетысай","Житикара","Зайсан",
  "Казалинск","Кандыагаш","Каражал","Каратау","Каркаралинск","Каскелен","Кентау","Косшы","Кульсары","Курчатов","Ленгер","Лисаковск",
  "Макинск","Мамлютка","Приозёрск","Риддер","Рудный","Сарань","Сарканд","Сарыагаш","Сатпаев","Сергеевка","Серебрянск","Степногорск",
  "Степняк","Тайынша","Талгар","Текели","Темир","Темиртау","Тобыл","Ушарал","Уштобе","Форт-Шевченко","Хромтау","Шалкар","Шар","Шардара",
  "Шахтинск","Шемонаиха","Шу","Щучинск","Эмба","Боралдай","Иргели","Отеген батыр","Узынагаш","Шамалган","Бесагаш","Туздыбастау",
  "Кордай","Жалагаш","Аулиеколь","Шиели","Мерке","Каратобе","Индербор","Махамбет","Мойынкум","Шетпе","Бейнеу","Жанакорган"].sort((a, b) => a.localeCompare(b, "ru")));
const cityKey = v => String(v || "").toLowerCase().replace(/ё/g, "е").replace(/[\s-]+/g, " ").trim();
function drawCities(q){
  const box = $("#rCityList"); if(!box) return;
  const k = cityKey(q), hit = k ? KZ_CITIES.filter(c => cityKey(c).startsWith(k)).concat(KZ_CITIES.filter(c => !cityKey(c).startsWith(k) && cityKey(c).includes(k))) : KZ_CITIES;
  box.innerHTML = hit.length ? hit.map(c => `<button type="button" data-city="${esc(c)}">${esc(c)}</button>`).join("")
    : `<div class="kv">Такого города нет в списке — можно оставить как написали, СДЭК проверит</div>`;
  box.hidden = false;
}
function pickCity(city){
  const inp = $("#rCity"), box = $("#rCityList"); clearTimeout(cityT);
  if(inp) inp.value = city; if(box) box.hidden = true;
  if(city === form().city) return sheetBody();
  DLV.pvzSel = null; DLV.pvz = []; DLV.pvzFor = ""; saveForm({ city, address: "" }); Promise.resolve(loadDlv(city)).then(() => { saveForm({}); sheetBody(); }); sheetBody();
}
function sheetInput(e){
  if(e.target.id === "rCity") drawCities(e.target.value);
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
  const pc = e.target.closest("[data-city]"); if(pc) return pickCity(pc.dataset.city);
  if(e.target.id !== "rCity" && $("#rCityList")) $("#rCityList").hidden = true;
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
  if(e.target.id === "rAgree"){
    CONSENT.cart = e.target.checked; const h = $("#rAgreeH"); if(h){ h.hidden = CONSENT.cart; h.classList.remove("bad"); }
    document.querySelectorAll("#rGo, [data-rgo]").forEach(b => CONSENT.cart ? b.removeAttribute("aria-disabled") : b.setAttribute("aria-disabled", "true"));
    return;
  }
  if(e.target.id === "rNews"){ CONSENT.news = e.target.checked; return; }   // необязательная — на оформление не влияет
  if(e.target.id === "rAll"){ if(e.target.checked) off.clear(); else lines().forEach(l => off.add(l.it.id)); saveOff(); renderRetailCart(); }
  const it = e.target.closest("[data-rsel]") && e.target.closest(".ritem");
  if(it){ e.target.checked ? off.delete(it.dataset.rid) : off.add(it.dataset.rid); saveOff(); renderRetailCart(); }
});
$("#drawerBody").addEventListener("click", e => {
  if(!$("#rFix") && !e.target.closest("[data-rlogin]")) return;
  if(e.target.closest("[data-rnext]")) return toCheckout();
  if(e.target.closest("[data-rback]")){ STEP = "cart"; renderRetailCart(); return; }
  if(e.target.closest("[data-rgo]") || e.target.id === "rGo"){ if(LEGAL() && !CONSENT.cart) return consentNeed("rAgree"); return submit(); }
  const pm = e.target.closest("[data-rpaym]");
  if(pm){ PAYM.v = pm.dataset.rpaym; document.querySelectorAll("[data-rpaym]").forEach(b => { const on = b === pm; b.classList.toggle("on", on); b.setAttribute("aria-checked", on); }); return; }
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
async function cardUrl(token){   // сумму считает сервер по заказу; здесь — только ссылка на страницу оплаты
  let j;
  try{ const r = await fetch(CONFIG.apiUrl + "/pay/card/init", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) }); j = await r.json(); }
  catch{ throw new Error("Нет связи с сервером"); }
  if(!j.ok || !j.url) throw new Error(j.error || "Оплата картой сейчас недоступна");
  return j.url;
}
function doneRetail(res, f, t, payErr){
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
      <li><b>${window.CARD_PAY ? "Оплатите картой" : "Подтвердим и пришлём ссылку на оплату"}</b><span>${window.CARD_PAY ? "Кнопка ниже или «Мои заказы» → «Оплатить». После оплаты заказ сразу уходит в сборку" : `Напишем в WhatsApp на ${esc(phone)} — обычно в течение 15 минут в рабочее время`}</span></li>
      <li><b>${f.ship === "pickup" ? "Можно забирать" : "Доставка"}</b><span>${esc(f.ship === "pickup" ? "Сообщим, когда заказ будет собран" : when || "")}</span></li>
    </ol>
    ${window.CARD_PAY && res.startToken ? `<button class="btn" id="rdCard" type="button">💳 Оплатить картой ${fmt(total)} ₸</button><div class="rhint mut" style="text-align:center;margin:6px 0 10px">Visa, Mastercard, Apple Pay, Google Pay · безопасная страница Freedom Pay</div><div class="err" id="rdErr">${esc(payErr || "")}</div>` : ""}
    ${pushBoxHTML()}
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
    <button class="btn${window.CARD_PAY && res.startToken ? " ghost" : ""}" id="rdOrders" type="button">Мои заказы</button>
    <button class="btn ghost" id="rdShop" type="button">Продолжить покупки</button>
  </div>`;
  if($("#rdCard")) $("#rdCard").onclick = async e => {   // оплата картой: сумму считает сервер по заказу, здесь — только переход на страницу оплаты
    const b = e.currentTarget, err = $("#rdErr"); b.disabled = true; b.textContent = "Открываем оплату…"; err.textContent = "";
    try{ location.href = await cardUrl(res.startToken); }
    catch(x){ err.textContent = x.message || "Нет связи с сервером"; b.disabled = false; b.textContent = `💳 Оплатить картой ${fmt(total)} ₸`; }
  };
  $("#rdOrders").onclick = () => openOrders();
  $("#rdShop").onclick = () => go("home");
  scrollTo({ top: 0 });
}
/* оплата картой не открылась — заказа ещё нет: «Ожидает оплаты», оплатить можно здесь или в «Моих заказах» (сутки) */
function pendingRetail(res, t, err){
  $("#drawerTitle").textContent = "Ожидает оплаты";
  $("#drawerBody").innerHTML = `<div class="done rdone">
    <div class="rdone-h"><div><h3>Осталось оплатить ${fmt(res.total || t.total)} ₸</h3><p>Заказ оформится и уйдёт в сборку сразу после оплаты</p></div></div>
    <button class="btn" id="rdCard" type="button">💳 Оплатить картой ${fmt(res.total || t.total)} ₸</button>
    <div class="rhint mut" style="text-align:center;margin:6px 0 10px">Visa, Mastercard, Apple Pay, Google Pay · безопасная страница Freedom Pay. Оплатить можно и позже — в «Моих заказах», в течение суток</div>
    <div class="err" id="rdErr">${esc(err || "")}</div>
    <button class="btn ghost" id="rdOrders" type="button">Мои заказы</button></div>`;
  $("#rdCard").onclick = async e => { const b = e.currentTarget; b.disabled = true; b.textContent = "Открываем оплату…"; $("#rdErr").textContent = "";
    try{ location.href = await cardUrl(res.payToken); }catch(x){ $("#rdErr").textContent = x.message; b.disabled = false; b.textContent = `💳 Оплатить картой ${fmt(res.total || t.total)} ₸`; } };
  $("#rdOrders").onclick = () => openOrders();
  scrollTo({ top: 0 });
}
/* Kaspi: сервер выставляет счёт на номер из заказа, здесь — экран «Счёт отправлен в Kaspi» и опрос статуса раз в 4 с.
   Оплачен — тот же экран «Спасибо! Заказ оплачен» в «Моих заказах». Отменён / истёк / ошибка — «Выставить ещё раз» или карта */
async function kaspiApi(path, token){
  let j;
  try{ const r = await fetch(CONFIG.apiUrl + "/pay/kaspi/" + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) }); j = await r.json(); }
  catch{ throw new Error("Нет связи с сервером"); }
  if(!j.ok) throw new Error(j.error || "Оплата Kaspi сейчас недоступна");
  return j;
}
async function kaspiScreen(title, body, token, total, again){
  const run = (body.dataset.kaspi = String(Date.now()));          // новый экран — прежний опрос останавливается
  const alive = () => body.isConnected && body.dataset.kaspi === run && body.querySelector("#kaspiBox");
  title.textContent = "Оплата Kaspi";
  const paint = (st, j = {}) => {
    const box = body.querySelector("#kaspiBox"); if(!box) return;
    const wait = st === "processing" || st === "pending" || st === "cancelling" || st === "load";
    const bad = { cancelled: "Счёт отменён в Kaspi", expired: "Время на оплату счёта истекло", error: j.error || "Kaspi не принял счёт" }[st];
    box.innerHTML = `<div class="rdone-h"><div><h3>${wait ? (st === "pending" ? "Счёт отправлен в Kaspi" : "Отправляем счёт в Kaspi…") : bad || esc(j.fail || "Не получилось выставить счёт")}</h3>
        <p>${fmt(j.total || total)} ₸${j.phone ? ` · на номер ${esc(j.phone)}` : ""}</p></div></div>
      ${wait ? `<ol class="rsteps"><li class="${st === "pending" ? "on" : ""}"><b>Откройте приложение Kaspi</b><span>Счёт от AMURA Cosmetics — в уведомлениях или в разделе «Платежи»</span></li>
        <li><b>Оплатите ${fmt(j.total || total)} ₸</b><span>Счёт действует сутки</span></li>
        <li><b>Заказ сразу уйдёт в сборку</b><span>Этот экран обновится сам — можно не закрывать</span></li></ol>
        <div class="rkwait"><span class="rkdot"></span>Ждём оплату в Kaspi…</div>`
      : `<button class="btn" type="button" data-rk-again>Выставить счёт ещё раз</button>${window.CARD_PAY ? `<button class="btn ghost" type="button" data-rk-card>💳 Оплатить картой</button>` : ""}`}
      <div class="rhint mut" style="text-align:center;margin:6px 0 10px">Заказ оформится после оплаты. Оплатить можно и позже — в «Моих заказах», в течение суток</div>
      <button class="btn ghost" type="button" data-rk-orders>Мои заказы</button>`;
  };
  body.innerHTML = `<div class="done rdone" id="kaspiBox"></div>`;
  paint("load");
  body.onclick = async e => {
    if(e.target.closest("[data-rk-orders]")){ body.dataset.kaspi = ""; return openOrders(); }
    if(e.target.closest("[data-rk-again]")) return kaspiScreen(title, body, token, total, true);
    const c = e.target.closest("[data-rk-card]");
    if(c){ c.disabled = true; c.textContent = "Открываем оплату…"; try{ location.href = await cardUrl(token); }catch(x){ toast(x.message); c.disabled = false; c.textContent = "💳 Оплатить картой"; } }
  };
  scrollTo({ top: 0 });
  let j;
  try{ j = await kaspiApi("init", token); }
  catch(x){ if(alive()) paint("fail", { fail: x.message }); return; }
  for(let i = 0; alive(); i++){
    if(j.draft && j.draft !== "new" || j.status === "paid"){   // оплачен — «Спасибо! Заказ оплачен» (номер заказа подставится, когда появится)
      body.dataset.kaspi = ""; window.PAY_RET = { r: "ok", n: token, how: "kaspi" }; return openOrders();
    }
    paint(j.status, j);
    if(!["processing", "pending", "cancelling"].includes(j.status)) return;
    await new Promise(r => setTimeout(r, i < 30 ? 4000 : 8000));
    if(!alive()) return;
    try{ j = { ...j, ...(await kaspiApi("status", token)) }; }catch{}
  }
}
const CHECK_BIG = '<svg class="rdone-ok" viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="22"/><path d="M14 24.5l7 7 13-14"/></svg>';

let busy = false;
async function submit(){
  if(busy) return;
  const err = $("#rErr"), f = form(), t = tot(f.ship);
  const fail = (m, sel) => { err.textContent = m; toast(m); if(sel && $(sel)){ $(sel).focus(); $(sel).scrollIntoView({ block: "center", behavior: "smooth" }); } };
  if(LEGAL() && !CONSENT.cart) return consentNeed("rAgree");
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
    orderKey: orderKey(), mode: "retail", ref: REF.code() || undefined, promo: PROMO ? PROMO.code : undefined,   // модуль «Источники»
    consent: LEGAL() ? true : undefined, news: LEGAL() ? CONSENT.news : undefined, pay: payWay() || undefined, payFirst: payNow() || undefined,   // оплата картой: заказ в МойСклад — только после оплаты   // факт и время согласия сервер пишет в комментарий заказа
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
    const df = { ...f, pvz: DLV.pvzSel, slot: DLV.slotSel, address: f.address || "" };
    if(res.draft){   // заказ появится после оплаты: сразу на страницу оплаты; не вышло — экран «Ожидает оплаты»
      orderKeyVal = ""; DLV.slotSel = null; STEP = "cart";
      t.ls.forEach(l => delete cart[l.it.id]); save("amura-cart2", cart); renderCartCount(); render();
      if(payWay() === "kaspi"){ $("#drawerBody").parentElement.classList.remove("rwide", "rstep2"); return kaspiScreen($("#drawerTitle"), $("#drawerBody"), res.payToken, res.total || t.total); }
      btns.forEach(b => { if(b.isConnected) b.textContent = "Открываем оплату…"; });
      let perr = "";
      try{ location.href = await cardUrl(res.payToken); return; }catch(x){ perr = x.message; }
      $("#drawerBody").parentElement.classList.remove("rwide", "rstep2");
      return pendingRetail(res, t, perr);
    }
    orderKeyVal = ""; DLV.slotSel = null; STEP = "cart";
    t.ls.forEach(l => delete cart[l.it.id]); save("amura-cart2", cart); renderCartCount(); render();   // заказанное убираем, остальное остаётся
    let payErr = "";
    if(res.total && Math.round(res.total) !== Math.round(t.total))   // сумма заказа не совпала с «Итого» в корзине — на оплату не ведём, показываем заказ
      payErr = `Сумма заказа — ${fmt(res.total)} ₸, а в корзине было ${fmt(t.total)} ₸. Проверьте заказ в «Моих заказах»; оплата спишет ${fmt(res.total)} ₸`;
    if(payNow() && res.startToken && !res.demo && !payErr){   // «Картой онлайн» — без промежуточного экрана сразу на страницу оплаты
      btns.forEach(b => { if(b.isConnected) b.textContent = "Открываем оплату…"; });
      try{ location.href = await cardUrl(res.startToken); return; }catch(x){ payErr = x.message; }
    }
    $("#drawerBody").parentElement.classList.remove("rwide", "rstep2");
    doneRetail(res, df, t, payErr);
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
/* ---------- отслеживание доставки (курьер Яндекс): лента в «Моих заказах», уведомления на сайте ---------- */
const TRK = "amura-r-track", STEPS = ["Принят", "Курьер назначен", "В пути", "Доставлен"];
const tm5 = s => { try{ return new Date(s).toLocaleTimeString("ru-RU", { timeZone: "Asia/Almaty", hour: "2-digit", minute: "2-digit" }); }catch{ return ""; } };
function trackHTML(tr){
  const bad = tr.step < 0;
  return `<div class="rtrk${bad ? " bad" : ""}"><div class="rtrk-l"><b>${esc(tr.label)}</b>${tr.from && tr.to && tr.step < 3 && !bad ? `<span>окно ${tm5(tr.from)}–${tm5(tr.to)}</span>` : ""}</div>
    ${bad ? "" : `<ol>${STEPS.map((t, i) => `<li class="${i <= tr.step ? "on" : ""}${i === tr.step ? " cur" : ""}"><i></i><span>${t}</span></li>`).join("")}</ol>`}
    ${tr.link ? `<a class="btn" href="${esc(tr.link)}" target="_blank" rel="noopener">Где курьер?</a>` : ""}</div>`;
}
let TRACK_ORDERS = null;
function trackPaint(orders){
  TRACK_ORDERS = orders || null;
  if(!orders) return;
  if(MO.orders === orders && $("#moList")) moList();       // отслеживание показывается в карточке заказа
  seenTracks(Object.fromEntries(orders.filter(o => o.track).map(o => [o.number, o.track])), false);
  meDot(false);
}
function seenTracks(tracks, notify){
  let seen = {}; try{ seen = JSON.parse(localStorage.getItem(TRK) || "{}"); }catch{}
  let changed = false;
  Object.entries(tracks).forEach(([n, tr]) => {
    if(seen[n] !== tr.code){ if(notify && seen[n] !== undefined){ toast(`Заказ № ${n}: ${tr.label}`); changed = true; } seen[n] = tr.code; }
  });
  try{ localStorage.setItem(TRK, JSON.stringify(seen)); }catch{}
  return changed;
}
function meDot(on){ document.querySelectorAll('[data-view="me"]').forEach(b => b.classList.toggle("rdot", on)); }
/* пока есть заказ в пути — раз в 1,5 минуты спрашиваем сервер; новый статус — всплывающее сообщение и точка на «Я» */
async function trackPoll(){
  if(!AUTH.token || document.visibilityState !== "visible") return;
  let seen = {}; try{ seen = JSON.parse(localStorage.getItem(TRK) || "{}"); }catch{}
  const active = Object.values(seen).some(c => !/deliver|return|cancel|fail/.test(c));
  if(!active && TRACK_ORDERS === null && trackPoll.done) return;
  trackPoll.done = true;
  try{
    const j = await api("/me/track");
    const tr = j.tracks || {};
    if(seenTracks(tr, true)){
      if(VIEW === "me" && TRACK_ORDERS){ TRACK_ORDERS.forEach(o => { if(tr[o.number]) o.track = tr[o.number]; }); trackPaint(TRACK_ORDERS); }
      else meDot(true);
    }
  }catch{}
}
setInterval(trackPoll, 90000); setTimeout(trackPoll, 4000);
document.addEventListener("visibilitychange", () => { if(document.visibilityState === "visible") trackPoll(); });
/* ---------- уведомления о доставке (Web Push): предложение включить — после заказа и в «Моих заказах» ---------- */
const PUSH_OK = "amura-r-push";
const pushCan = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const isIOS = () => /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const standalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
function pushOn(){ try{ return Notification.permission === "granted" && localStorage.getItem(PUSH_OK) === "1"; }catch{ return false; } }
function pushBoxHTML(){
  if(!AUTH.token || DEMO) return "";
  if(!pushCan()){
    return isIOS() && !standalone() ? `<div class="rpush"><b>🔔 Уведомления о доставке</b><span>Чтобы получать их на iPhone, добавьте сайт на экран «Домой»: кнопка «Поделиться» → «На экран Домой», затем откройте AMURA с иконки и включите уведомления.</span></div>` : "";
  }
  if(pushOn()) return "";
  if(Notification.permission === "denied") return `<div class="rpush"><b>🔔 Уведомления выключены</b><span>Разрешите уведомления для сайта в настройках браузера — сообщим, когда курьер заберёт заказ.</span></div>`;
  return `<div class="rpush"><b>🔔 Включите уведомления</b><span>Сообщим, когда курьер заберёт заказ и когда будет у вас — даже если сайт закрыт.</span><button type="button" class="btn" data-rpush>Включить</button></div>`;
}
async function pushEnable(btn){
  try{
    btn.disabled = true; btn.textContent = "Включаем…";
    if(await Notification.requestPermission() !== "granted") throw new Error("Уведомления не разрешены");
    const reg = await navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready);
    const { key } = await api("/me/push");
    const raw = atob(key.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((key.length + 3) % 4));
    const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: Uint8Array.from(raw, c => c.charCodeAt(0)) });
    await api("/me/push", { method: "POST", body: JSON.stringify({ sub: sub.toJSON() }) });
    try{ localStorage.setItem(PUSH_OK, "1"); }catch{}
    const box = btn.closest(".rpush"); if(box) box.innerHTML = "<b>🔔 Уведомления включены</b><span>Сообщим о каждом шаге доставки.</span>";
    toast("Уведомления включены");
  }catch(e){ btn.disabled = false; btn.textContent = "Включить"; toast(e.message || "Не получилось включить уведомления"); }
}
document.addEventListener("click", e => { const b = e.target.closest("[data-rpush]"); if(b) pushEnable(b); });

/* «Мои заказы»: неоплаченный заказ, где выбрана карта, — кнопка «Оплатить»; после возврата с оплаты — «Оплата получена» */
function payPaint(orders){
  const pr = window.PAY_RET;
  if(!pr || pr.r !== "ok" || pr.shown === "done") return;      // «Мои заказы» перерисовались (загрузка) — экран оплаты остаётся
  if(pr.draw) pr.draw(); else { pr.shown = true; paidScreen(pr); }
}
/* вернулись с оплаты — экран «Спасибо! Заказ оплачен» (как у WB): состав и сумма сразу, номер заказа подставляется, когда появится */
async function paidScreen(pr){
  const draft = /^D\d+_/.test(pr.n);
  let info = { number: draft ? "" : pr.n };
  const draw = pr.draw = () => {
    const o = !draft && MO.orders ? MO.orders.find(x => x.number === pr.n) : null;
    if(o) info = { number: o.number, total: o.sum, ship: o.ship, dlv: o.extra, items: o.items };
    if(VIEW !== "me" || !pr.shown) return;
    const its = info.items || [], cnt = its.reduce((s, it) => s + it.qty, 0);
    $("#meTitle").textContent = "Заказ оплачен";
    $("#meBody").innerHTML = `<div class="done rdone">
      <div class="rdone-h">${CHECK_BIG}<div><h3>Спасибо! Заказ оплачен</h3><p>${info.number ? `Заказ № ${esc(info.number)}` : `<span class="rpaid-n"></span>`}${info.total ? ` · ${fmt(info.total)} ₸` : ""}</p></div></div>
      <ol class="rsteps">
        <li class="on"><b>Оплата получена</b><span>${pr.how === "kaspi" ? `Оплачено через Kaspi${info.total ? " · " + fmt(info.total) + " ₸" : ""}` : `Списали ${info.total ? fmt(info.total) + " ₸ " : ""}с вашей карты`}</span></li>
        <li class="on"><b>Передали в сборку</b><span>Соберём и сообщим, когда заказ будет в пути</span></li>
        <li><b>${/самовывоз/i.test(info.ship || "") ? "Можно забирать" : "Доставка"}</b><span>${esc(info.ship || "")}</span></li>
      </ol>
      ${its.length ? `<div class="rcard rdone-d"><div class="mo-ths">${its.slice(0, 6).map(it => moPic(it, "mo-t")).join("")}</div>
        <div class="rrow"><span>${plTov(cnt)}</span><span>${fmt(its.reduce((s, it) => s + it.qty * it.price, 0))} ₸</span></div>
        <div class="rrow"><span>Доставка</span><span>${info.dlv ? fmt(info.dlv) + " ₸" : "бесплатно"}</span></div>
        <div class="rtot"><span>Итого</span><b>${fmt(info.total)} ₸</b></div></div>` : ""}
      ${pr.fail ? `<div class="kv">${esc(pr.fail)}</div>` : ""}
      <button class="btn" type="button" data-paid-orders>Мои заказы</button>
      <button class="btn ghost" type="button" data-paid-shop>Продолжить покупки</button></div>`;
  };
  draw();
  if(!draft) return;
  for(let i = 0; i < 20 && !info.number && !pr.fail; i++){   // номер заказа появляется за секунды после оплаты
    try{
      const r = await fetch(CONFIG.apiUrl + "/pay/card/status", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: pr.n }) });
      const j = await r.json();
      if(j.ok){ info = { ...j }; if(j.status === "failed") pr.fail = "Заказ оформит менеджер и свяжется с вами — оплата уже у нас"; draw(); }
    }catch{}
    if(!info.number && !pr.fail) await new Promise(r => setTimeout(r, i < 5 ? 1500 : 4000));
  }
}
document.addEventListener("click", e => {
  if(e.target.closest("[data-paid-orders]")){ const pr = window.PAY_RET; if(pr) pr.shown = "done"; MO.page = "orders"; renderMe(); try{ history.replaceState(null, "", "#orders"); }catch{} }
  if(e.target.closest("[data-paid-shop]")){ const pr = window.PAY_RET; if(pr) pr.shown = "done"; go("home"); }
});
document.addEventListener("click", e => {   // «Kaspi» в «Моих заказах» — экран счёта Kaspi вместо списка
  const b = e.target.closest("[data-rkaspi]"); if(!b) return;
  e.stopPropagation(); const mb = $("#viewMe .mo-top"); if(mb) mb.hidden = true;
  kaspiScreen($("#meTitle"), $("#meBody"), b.dataset.rkaspi, +b.dataset.sum || 0);
}, true);
document.addEventListener("click", async e => {   // «💳 Оплатить» в «Моих заказах» — на страницу оплаты по заказу
  const b = e.target.closest("[data-rpaynow]"); if(!b || b.disabled) return;
  const t = b.textContent; b.disabled = true; b.textContent = "Открываем оплату…";
  try{ location.href = await cardUrl(b.dataset.rpaynow); }catch(x){ toast(x.message); b.disabled = false; b.textContent = t; }
});
/* ---------- «Мои заказы» как у WB: карточки с фото, понятный статус, вкладки Активные / Завершённые, экран заказа ---------- */
const PAGES = { "#orders": "orders", "#seen": "seen" };   // экраны внутри «Я» со своим адресом
const MO = { p: null, orders: null, tab: "active", page: PAGES[location.hash] || "", keep: !!PAGES[location.hash] };
function moStatus(o){
  const s = o.status || "", pick = /самовывоз/i.test(o.ship || "");
  if(o.draft) return ["Ожидает оплаты", "wait"];
  if(/отмен/i.test(s)) return ["Отменён", "off"];
  if(/возврат/i.test(s)) return ["Возврат", "off"];
  if(/доставлен/i.test(s)) return ["Получен", "ok"];
  if(/отгруж/i.test(s)) return pick ? ["Получен", "ok"] : ["Передан в доставку", "go"];
  if(/собран/i.test(s)) return pick ? ["Готов к выдаче", "ok"] : ["Собирается", "go"];
  if(/подтвержд/i.test(s)) return o.paid ? ["Оплачен", "go"] : ["Собирается", "go"];
  if(o.payCard) return ["Ожидает оплаты", "wait"];
  return [o.paid ? "Оплачен" : "Принят", "go"];
}
const moDone = o => ["Получен", "Отменён", "Возврат"].includes(moStatus(o)[0]) && !(moStatus(o)[0] === "Получен" && o.track && o.track.step < 3);
const moPic = (it, cls) => { const c = byId(it.id), img = c && c.img; return `<span class="${cls}">${img ? `<img src="${esc(img)}" alt="" loading="lazy">` : `<i>${esc(((c && c.brand) || it.name || "A").charAt(0))}</i>`}${it.qty > 1 && cls === "mo-t" ? `<em>${it.qty}</em>` : ""}</span>`; };
const moTitle = o => o.draft ? "Ещё не оплачен" : `Заказ № ${esc(o.number)}`;
/* «Оплатить» у неоплаченного: заказ — картой; черновик — картой и/или Kaspi, что включено */
function moPay(o){
  if(!o.payCard) return "";
  const two = o.draft && window.KASPI_PAY && window.CARD_PAY;   // две кнопки — без суммы (она над кнопками), чтобы влезло в одну строку
  const card = (!o.draft || window.CARD_PAY) ? `<button type="button" class="btn rpaynow" data-rpaynow="${esc(o.payCard)}">💳 ${two ? "Картой" : `Оплатить ${fmt(o.sum)} ₸`}</button>` : "";
  const kaspi = o.draft && window.KASPI_PAY ? `<button type="button" class="btn rpaynow${card ? " ghost" : ""}" data-rkaspi="${esc(o.payCard)}" data-sum="${o.sum}">${two ? "Kaspi" : `Оплатить в Kaspi ${fmt(o.sum)} ₸`}</button>` : "";
  return card && kaspi ? `<div class="rpay2">${card}${kaspi}</div>` : card + kaspi;
}
function moCard(o, i){
  const [st, tone] = moStatus(o), its = o.items || [];
  return `<article class="rcard mo" data-mo-open="${i}" tabindex="0">
    <div class="mo-h"><span class="mo-chip ${tone}">${st}</span><span class="mo-d">от ${esc(o.date)}</span></div>
    <div class="mo-ths">${its.slice(0, 5).map(it => moPic(it, "mo-t")).join("")}${its.length > 5 ? `<span class="mo-t"><i>+${its.length - 5}</i></span>` : ""}</div>
    <div class="mo-f"><div><b>${moTitle(o)}</b>${o.ship ? `<small>${esc(o.ship)}</small>` : ""}</div><b class="mo-s">${fmt(o.sum)} ₸</b></div>
    ${o.track && o.track.step >= 0 && o.track.step < 3 ? `<div class="mo-trk"><b>${esc(o.track.label)}</b>${o.track.link ? `<a href="${esc(o.track.link)}" target="_blank" rel="noopener">Где курьер?</a>` : ""}</div>` : ""}
    ${moPay(o)}
  </article>`;
}
function moList(){
  const box = $("#orders"); if(!box || !MO.orders) return;
  box.classList.add("mo-list"); box.id = "orders";
  if(!MO.orders.length){ box.innerHTML = `<div class="notice" style="margin:0"><h3>Заказов пока нет</h3><p>Оформите первый заказ в каталоге — он появится здесь.</p></div>`; return; }
  const act = MO.orders.map((o, i) => [o, i]).filter(([o]) => !moDone(o)), done = MO.orders.map((o, i) => [o, i]).filter(([o]) => moDone(o));
  const list = MO.tab === "done" ? done : act;
  box.innerHTML = `<div class="rtabs n2 mo-tabs" role="tablist" id="moList">
      <button type="button" role="tab" aria-selected="${MO.tab !== "done"}" data-mo-tab="active">Активные${act.length ? `<small>${act.length}</small>` : ""}</button>
      <button type="button" role="tab" aria-selected="${MO.tab === "done"}" data-mo-tab="done">Завершённые${done.length ? `<small>${done.length}</small>` : ""}</button></div>
    ${list.length ? list.map(([o, i]) => moCard(o, i)).join("") : `<div class="kv" style="padding:14px 2px">${MO.tab === "done" ? "Завершённых заказов пока нет" : "Активных заказов нет — все получены"}</div>`}`;
}
function moOpen(i){
  const o = MO.orders[i]; if(!o) return;
  const [st, tone] = moStatus(o), goods = (o.items || []).reduce((s, it) => s + it.qty * it.price, 0);
  $("#meTitle").innerHTML = moTitle(o);
  const mb = $("#viewMe .mo-top"); if(mb) mb.hidden = true;            // в заказе своя кнопка «← Мои заказы»
  $("#meBody").innerHTML = `<div class="mo-det"><button type="button" class="back mo-back" data-mo-back>← Мои заказы</button>
    <section class="rcard"><div class="mo-h"><span class="mo-chip ${tone}">${st}</span><span class="mo-d">от ${esc(o.date)}</span></div>
      ${o.track ? trackHTML(o.track) : ""}
      ${moPay(o)}</section>
    <section class="rcard"><h3>Товары · ${fmt(o.count)} шт</h3>${(o.items || []).map(it => `<div class="mo-it">${moPic(it, "mo-p")}<div><b>${esc(it.name)}</b><small>${fmt(it.qty)} шт × ${fmt(it.price)} ₸</small></div><b>${fmt(it.qty * it.price)} ₸</b></div>`).join("")}</section>
    <section class="rcard"><h3>Получение</h3><div class="mo-ship">${esc(o.ship || "—")}</div>
      <div class="rrow"><span>Товары</span><span>${fmt(goods)} ₸</span></div>
      <div class="rrow"><span>Доставка</span><span class="${o.extra ? "" : "free"}">${o.extra ? fmt(o.extra) + " ₸" : "бесплатно"}</span></div>
      <div class="rtot"><span>Итого</span><b>${fmt(o.sum)} ₸</b></div></section>
    <div class="mo-act"><button type="button" class="btn" data-mo-repeat="${i}">Повторить заказ</button><button type="button" class="btn ghost" data-mo-support="${esc(o.number || "")}">Написать в поддержку</button></div>
  </div>`;
  scrollTo({ top: 0 });
}
function moRepeat(i){
  let added = 0, missing = 0;
  (MO.orders[i].items || []).forEach(it => { const c = byId(it.id); if(c){ cart[c.id] = Math.min(c.qty, it.qty); added++; } else missing++; });
  save("amura-cart2", cart); renderCartCount(); render();
  toast(added ? `Добавлено в корзину: ${added}${missing ? `, нет в наличии: ${missing}` : ""}` : "Этих товаров сейчас нет в наличии");
  if(added) go("cart");
}
document.addEventListener("click", e => {
  if(VIEW !== "me" || !MO.orders) return;
  if(e.target.closest("[data-rpaynow], a")) return;
  const t = e.target.closest("[data-mo-tab]"); if(t){ MO.tab = t.dataset.moTab; return moList(); }
  if(e.target.closest("[data-mo-back]")){ paintMe(MO.p, MO.orders); return scrollTo({ top: 0 }); }
  const r = e.target.closest("[data-mo-repeat]"); if(r) return moRepeat(+r.dataset.moRepeat);
  const sp = e.target.closest("[data-mo-support]"); if(sp){ if(window.openSupport) openSupport(sp.dataset.moSupport ? `Вопрос по заказу № ${sp.dataset.moSupport}: ` : ""); return; }
  const c = e.target.closest("[data-mo-open]"); if(c) moOpen(+c.dataset.moOpen);
});
const _renderMo = render;   // каталог загрузился позже списка — перерисовать миниатюры заказов с фото
render = function(){ const r = _renderMo.apply(this, arguments); if(VIEW === "me" && $("#moList")) moList(); return r; };
document.addEventListener("keydown", e => { const c = e.key === "Enter" && e.target.closest && e.target.closest("[data-mo-open]"); if(c) moOpen(+c.dataset.moOpen); });

const _paintMe = paintMe;
paintMe = function(p, orders){
  _paintMe(p, orders); markRetail();
  MO.p = p; MO.orders = orders;
  if(orders && orders.length && !orders.some(o => !moDone(o))) MO.tab = "done";
  const top = $("#viewMe .back[data-home]"), ord = !!MO.page;   // на экранах «Мои заказы» и «Вы смотрели» вместо «← В каталог» — «← Назад» в «Я»
  if(top) top.hidden = ord;
  const mb = $("#viewMe .mo-top"); if(mb && !ord) mb.remove();
  if(ord && !mb) $("#meTitle").insertAdjacentHTML("beforebegin", '<button type="button" class="back mo-top" data-mo-home>← Назад</button>');
  else if(ord) mb.hidden = false;
  if(MO.page === "orders") moPage(); else if(MO.page === "seen") seenPage(); else { moEntry(); favEntry(); seenBlock(); supportEntry(); }
  let want = false; try{ want = sessionStorage.getItem(WANT) === "1"; }catch{}
  if(want && AUTH.token && p && p.name){ try{ sessionStorage.removeItem(WANT); }catch{} toast("Вы вошли — оформляем заказ"); NEXT = "checkout"; go("cart"); }
};

/* «Мои заказы» — отдельный экран amura.kz/#orders (как на WB): «← Назад» и системный «назад» — в «Я»; ниже «Вам может понравиться».
   В «Я» — только пункт «Мои заказы» со счётчиком активных */
function moEntry(){
  const box = $("#orders"); if(!box) return;
  const h = box.previousElementSibling; if(h && h.classList.contains("page-h")) h.remove();
  const n = MO.orders ? MO.orders.filter(o => !moDone(o)).length : 0, m = n % 10, hh = n % 100;
  const sub = !MO.orders ? "Загружаем…" : n ? `${n} ${m === 1 && hh !== 11 ? "активный" : "активных"} · в работе` : MO.orders.length ? "Все заказы получены" : "Заказов пока нет";
  box.remove();
  const html = `<button type="button" class="rsup rmo" data-mo-page><span class="rsup-i" aria-hidden="true">📦</span><span><b>Мои заказы</b><small>${sub}</small></span>${n ? `<em class="rmo-n">${n}</em>` : ""}<span class="rsup-a" aria-hidden="true">›</span></button>`;
  const prof = $("#meBody #profile"); if(prof) prof.insertAdjacentHTML("afterend", html); else $("#meBody").insertAdjacentHTML("afterbegin", html);
}
function moPage(){
  $("#meTitle").textContent = "Мои заказы";
  $("#meBody").innerHTML = `<div class="list" id="orders"><div class="kv">Загружаем…</div></div>`;
  moList(); trackPaint(MO.orders); payPaint(MO.orders);
  const oh = $("#orders"); if(oh && MO.orders && MO.orders.length) oh.insertAdjacentHTML("beforebegin", pushBoxHTML());
}
function openPage(page){
  MO.page = page;
  if(VIEW === "me" && MO.p){
    try{ history.pushState({ mo: 1 }, "", "#" + page); }catch{}
    paintMe(MO.p, MO.orders); scrollTo({ top: 0 });
  } else { MO.keep = true; go("me"); }
}
function openOrders(){ openPage("orders"); }
/* «Избранное · N товаров» и «Вы смотрели» (история просмотров только в браузере — amura-seen, её пишет hits.js) */
function favEntry(){
  if($("#meBody .rfav")) return;
  const n = FAV.length, m = n % 10, h = n % 100, w = m === 1 && h !== 11 ? "товар" : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? "товара" : "товаров";
  const html = `<button type="button" class="rsup rfav" data-go-fav><span class="rsup-i" aria-hidden="true">🤍</span><span><b>Избранное</b><small>${n ? `${n} ${w}` : "Пока пусто"}</small></span><span class="rsup-a" aria-hidden="true">›</span></button>`;
  const after = $("#meBody .rmo") || $("#meBody #profile"); if(after) after.insertAdjacentHTML("afterend", html);
}
const seenItems = () => [...new Set(load("amura-seen", []))].map(byId).filter(Boolean);   // byId — только товары в наличии
function seenBlock(){
  const L = seenItems().slice(0, 20), old = $("#meBody .rseen");
  if(old) old.remove();
  if(!L.length) return;
  const html = `<section class="rseen"><button type="button" class="rseen-h" data-seen-page>Вы смотрели <span aria-hidden="true">›</span></button><div class="grid rseen-row">${L.map(cardHTML).join("")}</div></section>`;
  const after = $("#meBody .rfav") || $("#meBody .rmo") || $("#meBody #profile"); if(after) after.insertAdjacentHTML("afterend", html);
}
function seenPage(){
  const L = seenItems();
  $("#meTitle").textContent = "Вы смотрели";
  $("#meBody").innerHTML = L.length ? `<div class="grid rseen-all">${L.map(cardHTML).join("")}</div><button type="button" class="btn ghost rseen-clr" data-seen-clear>Очистить историю</button>`
    : `<div class="notice" style="margin:0"><h3>Здесь пока пусто</h3><p>Открывайте товары в каталоге — они появятся здесь.</p></div>`;
}
window.moExclude = () => MO.page === "orders" && MO.orders ? MO.orders.filter(o => !moDone(o)).flatMap(o => (o.items || []).map(i => i.id))
  : MO.page === "seen" ? seenItems().map(i => i.id) : MO.page === "" && MO.p ? seenItems().slice(0, 20).map(i => i.id) : null;   // подборка — без того, что уже на экране
const _goMo = go;      // уход в другой раздел или нажатие «Я» — снова кабинет; открытие #orders — экран заказов
go = function(v){
  if(!MO.keep) MO.page = ""; MO.keep = false;
  const r = _goMo.apply(this, arguments);
  if(v === "me" && MO.page) try{ history.replaceState(history.state, "", "#" + MO.page); }catch{}
  return r;
};
addEventListener("popstate", () => {
  if(VIEW !== "me" || !MO.p) return;
  const want = PAGES[location.hash] || "";
  if(want !== MO.page){ MO.page = want; paintMe(MO.p, MO.orders); scrollTo({ top: 0 }); }
});
document.addEventListener("click", e => {
  if(e.target.closest("[data-mo-page]")) return openOrders();
  if(e.target.closest("[data-seen-page]")) return openPage("seen");
  if(e.target.closest("[data-go-fav]")) return go("fav");
  if(e.target.closest("[data-seen-clear]")){ save("amura-seen", []); toast("История просмотров очищена"); paintMe(MO.p, MO.orders); return; }
  const sc = e.target.closest(".rseen [data-id], .rseen-all [data-id]");     // карточки «Вы смотрели»: открыть, ♡, корзина — как в каталоге
  if(sc && !e.target.closest("[data-radd]")) onBuyClick(e);
  if(e.target.closest("[data-mo-home]")){ if(history.state && history.state.mo) history.back(); else { MO.page = ""; paintMe(MO.p, MO.orders); try{ history.replaceState(null, "", "#me"); }catch{} scrollTo({ top: 0 }); } }
});
if(MO.keep && typeof go === "function") setTimeout(() => { if(MO.page && VIEW !== "me"){ MO.keep = true; go("me", true); } }, 0);   // открыли amura.kz/#orders или #seen

/* «Поддержка» в кабинете — только после входа: чат «Поддержка AMURA», отвечает ИИ по правилам поддержки или менеджер */
function supportEntry(){
  if(!$("#meBody") || $("#meBody [data-support]")) return;
  const prof = $("#meBody #profile"), at = $("#meBody [data-coop]") || $("#meBody #logout");
  const html = `<button type="button" class="rsup" data-support><span class="rsup-i" aria-hidden="true">💬</span><span><b>Поддержка</b><small>Вопрос о заказе, доставке, оплате или товаре — ответим в чате</small></span><span class="rsup-a" aria-hidden="true">›</span></button>`;
  const after = $("#meBody .rseen") || $("#meBody .rfav") || $("#meBody .rmo") || prof;   // под «Вы смотрели» / «Избранным» / «Моими заказами»
  if(after) after.insertAdjacentHTML("afterend", html);
  else if(at) at.insertAdjacentHTML("beforebegin", html); else $("#meBody").insertAdjacentHTML("beforeend", html);
  $("#meBody [data-support]").onclick = () => { if(window.openSupport) openSupport(""); };
}
renderCart = renderRetailCart;
const _shipName = dlvShipName;
dlvShipName = function(f){ return f.ship === "express" ? "Срочный курьер по Алматы (Яндекс Экспресс) — " + (f.address || "") : _shipName(f); };
/* розница: вход через Telegram-бот — главный, по SMS — запасной (слово владельца 31.10.2026). «Запомнить меня» и согласие
   с офертой — над кнопкой Telegram: относятся к обоим способам; без галочки не открывается ни бот, ни код по SMS */
const _renderAuth = renderAuth;
renderAuth = function(){
  _renderAuth();
  const tgb = $("#tgLogin"), send = $("#aSend");
  if(tgb && !$("#aRemember")){
    tgb.insertAdjacentHTML("beforebegin", `<label class="rremember"><input type="checkbox" id="aRemember" ${remember() ? "checked" : ""}><span>Запомнить меня<small>Снимите на чужом телефоне — вход сбросится, когда закроете браузер</small></span></label>`);
    $("#aRemember").onchange = e => { try{ localStorage.setItem(REM, e.target.checked ? "1" : "0"); }catch{} };
  }
  if(tgb && LEGAL() && !$("#aAgree")){   // согласие — до входа (и остаётся отмеченным на шаге с кодом SMS)
    tgb.insertAdjacentHTML("beforebegin", consentHTML("aAgree", CONSENT.auth));
    const sync = () => [tgb, send].forEach(b => { if(b) CONSENT.auth ? b.removeAttribute("aria-disabled") : b.setAttribute("aria-disabled", "true"); });
    sync();
    $("#aAgree").onchange = e => { CONSENT.auth = e.target.checked; const h = $("#aAgreeH"); h.hidden = CONSENT.auth; h.classList.remove("bad"); sync(); };
    $("#af").addEventListener("submit", e => { if(!CONSENT.auth){ e.preventDefault(); e.stopImmediatePropagation(); consentNeed("aAgree"); } }, true);
    tgb.parentElement.addEventListener("click", e => { if(e.target.closest("#tgLogin") && !CONSENT.auth){ e.preventDefault(); e.stopImmediatePropagation(); consentNeed("aAgree"); } }, true);
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
  if(path === "/auth/sms/verify" && LEGAL() && CONSENT.auth && opts && opts.body){   // сервер запишет согласие клиенту
    try{ opts = { ...opts, body: JSON.stringify({ ...JSON.parse(opts.body), consent: true }) }; }catch{}
  }
  if(path.startsWith("/auth/tg/poll") && LEGAL() && CONSENT.auth) path += "&consent=1";
  try{ return await _api(path, opts); }
  catch(e){ if(path === "/auth/sms/send" && /telegram/i.test(e.message)) throw new Error("Вход по SMS временно недоступен. Попробуйте позже или напишите нам в WhatsApp"); throw e; }
};
const _go = go;
go = function(v, keep){ if(v === "cart"){ STEP = NEXT || "cart"; NEXT = ""; } return _go(v, keep); };
dlvRefresh = () => { paint(); const n = $("#rsNote"); if(n) n.textContent = courierNote(); };
document.addEventListener("keydown", e => { if(e.key === "Escape" && $("#rSheet") && !$("#rSheet").hidden) closeSheet(); });
/* город в шапке, как у WB: «📍 Алматы» — выбор сразу на сайте; тот же город берут корзина и сроки на карточках */
const PIN_S = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-6.5-6.2-6.5-11.2a6.5 6.5 0 0 1 13 0C18.5 14.8 12 21 12 21Z"/><circle cx="12" cy="9.8" r="2.3"/></svg>';
function topCity(){
  const w = $("header.top .wrap"); if(!w) return;
  let b = $("#rTopCity");
  if(!b){ const row = document.createElement("div"); row.className = "rtopc"; row.innerHTML = `<button type="button" id="rTopCity" aria-haspopup="dialog"></button>`; w.prepend(row); b = $("#rTopCity"); b.onclick = openCityPick; }
  const c = form().city;
  b.innerHTML = `${PIN_S}<span>${c ? esc(c) : "Выберите город"}</span>`;
  b.setAttribute("aria-label", c ? "Город доставки: " + c + ". Изменить" : "Выбрать город доставки");
}
function openCityPick(){
  let el = $("#rCitySheet");
  if(!el){ el = document.createElement("div"); el.id = "rCitySheet"; el.className = "rsheet"; document.body.appendChild(el);
    el.addEventListener("click", e => {
      if(e.target === el || e.target.closest("[data-rcclose]")) return closeCityPick();
      const pc = e.target.closest("[data-tcity]"); if(pc) return setTopCity(pc.dataset.tcity);
      if(e.target.closest("#rcGeo")){ const g = $("#rcGeo"); g.disabled = true; g.lastChild.textContent = "Определяем…";
        return geoCity().then(c => { if(c) setTopCity(c); else { g.disabled = false; g.lastChild.textContent = "Не получилось — выберите из списка"; } }); }
      if(e.target.id === "rcOk"){ const v = $("#rcQ").value.trim(); if(v) setTopCity(v); else $("#rcQ").focus(); }
    });
    el.addEventListener("input", e => { if(e.target.id === "rcQ") drawTopCities(e.target.value); });
    el.addEventListener("keydown", e => { if(e.target.id === "rcQ" && e.key === "Enter"){ e.preventDefault(); const v = e.target.value.trim(); const first = $("#rcList [data-tcity]"); if(first && v) setTopCity(first.dataset.tcity); else if(v) setTopCity(v); } });
  }
  el.innerHTML = `<div class="rsbox" role="dialog" aria-modal="true" aria-label="Ваш город">
    <div class="rsh"><b>Ваш город</b><button type="button" class="rx" data-rcclose aria-label="Закрыть">×</button></div>
    <div class="kv">Покажем способы и сроки доставки для вашего города</div>
    <input id="rcQ" autocomplete="off" placeholder="Начните вводить: Алматы, Астана…" aria-label="Город">
    ${navigator.geolocation ? `<button type="button" class="rgeo" id="rcGeo">${PIN_S}<span>Определить автоматически</span></button>` : ""}
    <div class="rcl" id="rcList"></div>
    <button type="button" class="btn" id="rcOk">Готово</button></div>`;
  el.hidden = false; document.body.classList.add("rsheet-open");
  drawTopCities(""); setTimeout(() => { const i = $("#rcQ"); if(i) i.focus(); }, 50);
}
function drawTopCities(q){
  const box = $("#rcList"); if(!box) return;
  const k = cityKey(q), cur = form().city;
  const hit = k ? KZ_CITIES.filter(c => cityKey(c).startsWith(k)).concat(KZ_CITIES.filter(c => !cityKey(c).startsWith(k) && cityKey(c).includes(k))) : KZ_CITIES;
  box.innerHTML = hit.length ? hit.map(c => `<button type="button" data-tcity="${esc(c)}" class="${c === cur ? "on" : ""}">${esc(c)}${c === cur ? " ✓" : ""}</button>`).join("")
    : `<div class="kv">Такого города нет в списке — нажмите «Готово», оставим как написали</div>`;
}
function closeCityPick(){ const el = $("#rCitySheet"); if(el) el.hidden = true; if(!($("#rSheet") && !$("#rSheet").hidden)) document.body.classList.remove("rsheet-open"); }
function setTopCity(city){
  closeCityPick();
  if(city !== form().city){ DLV.pvzSel = null; DLV.pvz = []; DLV.pvzFor = ""; saveForm({ city, address: "" }); loadDlv(city); }
  topCity();
  try{ if(VIEW === "cart") paint(); else render(); }catch{}
}
const _saveForm = saveForm;
saveForm = function(patch){ const f = _saveForm(patch); if(patch && "city" in patch) topCity(); return f; };
/* как в Золотом Яблоке: при первом заходе определяем город по геопозиции и спрашиваем «Ваш город — …? Да, верно / Нет, другой».
   Отказ в доступе или ошибка — предлагаем Алматы. Спрашиваем один раз; потом город меняют в шапке. */
function geoPos(){
  return new Promise(ok => {
    if(!navigator.geolocation) return ok(null);
    try{ navigator.geolocation.getCurrentPosition(p => ok(p.coords), () => ok(null), { timeout: 10000, maximumAge: 3600000 }); }catch{ ok(null); }
  });
}
async function geoCity(){
  const c = await geoPos(); if(!c) return "";
  try{
    const j = await getJSON(`${CONFIG.apiUrl}/geo/city?lat=${c.latitude.toFixed(4)}&lon=${c.longitude.toFixed(4)}`, 12000);
    const n = String((j && j.city) || "").trim(); if(!n) return "";
    return KZ_CITIES.find(x => cityKey(x) === cityKey(n)) || n;
  }catch{ return ""; }
}
function askCity(city, found){
  let el = $("#rGeoAsk");
  if(!el){ el = document.createElement("div"); el.id = "rGeoAsk"; el.className = "rsheet"; document.body.appendChild(el);
    el.addEventListener("click", e => {
      if(e.target.closest("[data-gyes]")){ closeAsk(); setTopCity(el.dataset.city); }
      else if(e.target.closest("[data-gno]") || e.target === el){ closeAsk(); openCityPick(); }
    });
  }
  el.dataset.city = city;
  el.innerHTML = `<div class="rsbox" role="dialog" aria-modal="true" aria-label="Ваш город">
    <div class="kv">${found ? "Ваш город" : "Доставляем по всему Казахстану. Ваш город"}</div>
    <div class="rgq">${esc(city)}?</div>
    <div class="rgb"><button type="button" class="btn ghost" data-gno>Нет, другой</button><button type="button" class="btn" data-gyes>Да, верно</button></div></div>`;
  el.hidden = false; document.body.classList.add("rsheet-open");
}
function closeAsk(){ const el = $("#rGeoAsk"); if(el) el.hidden = true; if(!($("#rSheet") && !$("#rSheet").hidden)) document.body.classList.remove("rsheet-open"); }
async function geoFirst(){
  if(form().city || load("geoAsked", 0)) return;
  save("geoAsked", 1);
  const c = await geoCity();
  if(form().city) return;                       // пока ждали — человек уже выбрал сам
  askCity(c || "Алматы", !!c);
}
topCity();
setTimeout(geoFirst, 1200);
markRetail();
if(VIEW === "cart") renderCart();
if(VIEW === "me" && !AUTH.token) renderAuth();
})();
