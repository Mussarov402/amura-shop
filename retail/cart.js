/* Розничный сайт (amura.kz/shop): оформление заказа как на WB.
   Только для розницы — make_shop.py вставляет этот файл в shop/index.html; оптовый index.html не меняется.
   Способ получения вкладками (пункт выдачи / курьер / самовывоз), адрес — строкой со стрелкой (выбор в окне),
   срок и цена доставки сразу, товары, получатель, способ оплаты, итог справа (компьютер) или снизу (телефон).
   Заказать можно только после входа или регистрации; покупатель помечается в МойСклад меткой «розница». */
(function(){
if(!window.AMURA_RETAIL) return;
const MON = ["января","февраля","марта","апреля","мая","июня","июля","августа","сентября","октября","ноября","декабря"];
const WANT = "amura-r-checkout";                 // «после входа вернуться к оформлению»
const TABS = [["cdek", "Пункт выдачи"], ["courier", "Курьер"], ["pickup", "Самовывоз"]];
const plural = (n, a, b, c) => { const m = n % 10, h = n % 100; return m === 1 && h !== 11 ? a : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? b : c; };
const PIN = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-6.5-6.2-6.5-11.2a6.5 6.5 0 0 1 13 0C18.5 14.8 12 21 12 21Z"/><circle cx="12" cy="9.8" r="2.3"/></svg>';
const CHEV = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9.5 6 6 6-6 6"/></svg>';
const CARD = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M3 9.5h18M7 14.5h4"/></svg>';

function form(){
  const f = load("amura-form", {}), pr = AUTH.profile || {};
  if(!["cdek", "courier", "pickup"].includes(f.ship)) f.ship = "cdek";
  f.name = f.name || pr.name || ""; f.city = f.city || pr.city || "";
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
  if(f.ship === "courier" && o.almaty){ const s = DLV.slotSel || (o.slots || [])[0]; return s ? `${slotDay(s)}, ${s.from}–${s.to}` : "Курьер"; }
  const m = method(f.ship); if(!m || !m.days) return f.ship === "cdek" ? "Пункт выдачи" : "Курьер";
  return rangeLabel(m.days[0] + 1, m.days[1] + 1);
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
  else { title = f.address || "Укажите адрес доставки"; sub = city + (DLV.opts && !DLV.opts.almaty ? " · СДЭК до двери" : ""); }
  return `<button type="button" class="raddr" data-raddr>${PIN}<span><b class="${/^(Укажите|Выберите)/.test(title) ? "ph" : ""}">${esc(title)}</b><small>${esc(sub)}</small></span>${CHEV}</button>`;
}
function slotsHTML(f){
  const o = DLV.opts;
  if(f.ship !== "courier" || !o || !o.almaty || !(o.slots || []).length) return "";
  return `<div class="rslots-h">Когда доставить</div><div class="rslots">${o.slots.slice(0, 9).map(s => {
    const on = DLV.slotSel && DLV.slotSel.date === s.date && DLV.slotSel.from === s.from;
    return `<button type="button" class="${on ? "on" : ""}" data-rslot="${esc(s.date + "|" + s.from)}"><b>${esc(slotDay(s))}</b><small>${esc(s.from)}–${esc(s.to)}</small></button>`; }).join("")}</div>`;
}
function dlvCard(f, t){
  const tabs = TABS.map(([id, name]) => {
    const p = id === "pickup" ? 0 : dlvPrice(id, t.goods);
    return `<button type="button" role="tab" aria-selected="${f.ship === id}" data-rm="${id}">${name}${p === null ? "" : `<small>${priceTxt(p)}</small>`}</button>`; }).join("");
  const e = eta(f), w = t.dlv === null ? (f.city ? "считаем…" : "") : priceTxt(t.dlv);
  return `<div class="rtabs" role="tablist">${tabs}</div>${addrRow(f)}
    <div class="reta"><b>${esc(e)}${w ? `, <span class="${t.dlv === 0 ? "free" : ""}">${w}</span>` : ""}</b><span>${fmt(t.count)} шт</span></div>
    ${DLV.opts && DLV.opts.warn ? `<div class="kv">${esc(DLV.opts.warn)}</div>` : ""}
    <div class="rthumbs">${t.ls.slice(0, 8).map(l => `<span>${l.it.img ? `<img src="${esc(l.it.img)}" alt="" loading="lazy">` : `<i>${esc((l.it.brand || l.it.name || "A").charAt(0))}</i>`}${l.q > 1 ? `<em>${l.q}</em>` : ""}</span>`).join("")}${t.ls.length > 8 ? `<span><i>+${t.ls.length - 8}</i></span>` : ""}</div>
    ${slotsHTML(f)}`;
}
function totalsHTML(f, t){
  const o = DLV.opts, rest = o && o.freeFrom && t.goods < o.freeFrom && f.ship === "cdek" ? o.freeFrom - t.goods : 0;
  const restC = o && o.freeFrom && t.goods < o.freeFrom && f.ship === "courier" && o.almaty ? o.freeFrom - t.goods : 0;
  const login = !AUTH.token;
  return `<div class="rtot"><span>Итого</span><b>${fmt(t.total)} ₸</b></div>
    <div class="rrow"><span>${fmt(t.count)} ${plural(t.count, "товар", "товара", "товаров")} на сумму</span><span>${fmt(t.goods)} ₸</span></div>
    <div class="rrow"><span>Доставка</span><span class="${t.dlv === 0 ? "free" : ""}">${t.dlv === null ? (f.city ? "считаем…" : "укажите город") : priceTxt(t.dlv)}</span></div>
    ${rest || restC ? `<div class="rhint">До бесплатной доставки — ещё ${fmt(rest || restC)} ₸</div>` : ""}
    ${f.ship === "courier" && o && !o.almaty ? `<div class="rhint mut">Доставка до двери СДЭК всегда платная</div>` : ""}
    <div class="err" id="rErr"></div>
    <button type="button" class="btn rgo" id="rGo">${login ? "Войти и заказать" : "Заказать"}</button>
    ${login ? `<div class="rhint mut">Чтобы оформить заказ, войдите или зарегистрируйтесь — по номеру телефона или через Telegram, без пароля</div>` : ""}`;
}

function renderRetailCart(){
  const title = $("#drawerTitle");
  $("#drawerBody").parentElement.classList.add("rwide");
  const f = form(), t = totals(f.ship);
  if(!t.ls.length){
    title.textContent = "Корзина";
    $("#drawerBody").innerHTML = `<div class="notice" style="border:0"><h3>Корзина пуста</h3><p>Добавьте товары из каталога — корзина сохранится, даже если закрыть страницу.</p></div>`;
    return;
  }
  title.innerHTML = `Оформление заказа<small class="rsub" id="rSub">${fmt(t.count)} ${plural(t.count, "товар", "товара", "товаров")}, ${fmt(t.total)} ₸</small>`;
  const pr = AUTH.profile || {};
  $("#drawerBody").innerHTML = `<div class="rco">
    <div class="rmain">
      <section class="rcard" id="rDlv">${dlvCard(f, t)}</section>
      <section class="rcard"><h3>Товары</h3>
        <div id="cartLines">${t.ls.map(l => `<div class="line" data-id="${esc(l.it.id)}">
          <div class="n">${esc(l.it.name)}</div><div class="p">${fmt(l.price * l.q)} ₸</div>
          <div class="stepper"><button data-dec aria-label="Меньше">−</button><input type="number" inputmode="numeric" min="0" max="${l.it.qty}" value="${l.q}" aria-label="Количество"><button data-inc aria-label="Больше">+</button></div>
          <div class="kv" style="text-align:right">${fmt(l.price)} ₸ / шт</div></div>`).join("")}</div>
        <button class="rlink" id="clear" type="button">Очистить корзину</button></section>
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
    <aside class="rcard rside" id="rTot">${totalsHTML(f, t)}</aside></div>`;
  if(f.city && (!DLV.opts || DLV.city !== f.city)) loadDlv(f.city);
}
function paint(){
  if(VIEW !== "cart" || !$("#rDlv")) return;
  const f = form(), t = totals(f.ship);
  $("#rDlv").innerHTML = dlvCard(f, t);
  $("#rTot").innerHTML = totalsHTML(f, t);
  const s = $("#rSub"); if(s) s.textContent = `${fmt(t.count)} ${plural(t.count, "товар", "товара", "товаров")}, ${fmt(t.total)} ₸`;
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
  return f.city && o ? `${o.almaty ? "Доставит курьер Яндекс в выбранный интервал" : "Доставит курьер СДЭК до двери"} · ${priceTxt(dlvPrice("courier", totals("courier").goods))}` : "";
}
function closeSheet(){ const el = $("#rSheet"); if(el) el.hidden = true; document.body.classList.remove("rsheet-open"); paint(); }
let cityT = 0;
function sheetInput(e){
  if(e.target.id === "rCity"){ clearTimeout(cityT); cityT = setTimeout(() => {
    const city = e.target.value.trim(); if(city === form().city) return;
    DLV.pvzSel = null; DLV.pvz = []; DLV.pvzFor = ""; saveForm({ city, address: "" }); loadDlv(city); sheetBody(); }, 600); }
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
$("#drawerBody").addEventListener("click", e => {
  if(!$("#rDlv") && !e.target.closest("[data-rlogin]")) return;
  const tab = e.target.closest("[data-rm]");
  if(tab){ saveForm({ ship: tab.dataset.rm }); paint(); const f = form(); if(f.ship !== "pickup" && (!f.city || (f.ship === "cdek" && !DLV.pvzSel && !f.address) || (f.ship === "courier" && !f.address))) openSheet(); return; }
  if(e.target.closest("[data-raddr]")) return openSheet();
  const sl = e.target.closest("[data-rslot]");
  if(sl && DLV.opts){ const [date, from] = sl.dataset.rslot.split("|"); DLV.slotSel = DLV.opts.slots.find(s => s.date === date && s.from === from) || null; saveForm({}); paint(); return; }
  if(e.target.closest("[data-rlogin]")) return toLogin();
  if(e.target.id === "rGo") submit();
});
$("#drawerBody").addEventListener("input", e => {
  if(e.target.id === "rName") saveForm({ name: e.target.value.trim() });
  if(e.target.id === "rPhone") saveForm({ phone: e.target.value.trim() });
});
function toLogin(){ try{ sessionStorage.setItem(WANT, "1"); }catch{} go("me"); }

let busy = false;
async function submit(){
  if(busy) return;
  const err = $("#rErr"), f = form(), t = totals(f.ship);
  const fail = (m, sel) => { err.textContent = m; if(sel && $(sel)){ $(sel).focus(); $(sel).scrollIntoView({ block: "center", behavior: "smooth" }); } };
  if(!AUTH.token) return toLogin();
  const digits = normPhone(String(f.phone).replace(/\D/g, ""));
  if(!f.name) return fail("Укажите имя получателя", "#rName");
  if(!/^7\d{10}$/.test(digits)) return fail("Укажите телефон получателя", "#rPhone");
  if(f.ship !== "pickup" && !f.city) return openSheet();
  if(f.ship === "cdek" && !DLV.pvzSel && !f.address){ fail("Выберите пункт выдачи"); return openSheet(); }
  if(f.ship === "courier" && !f.address){ fail("Укажите адрес доставки"); return openSheet(); }
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
  const btn = $("#rGo"); busy = true; btn.disabled = true; btn.textContent = "Отправляем…";
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
    $("#drawerBody").parentElement.classList.remove("rwide");
    showDone(res, { ...f, contact: "wa", pvz: DLV.pvzSel, slot: DLV.slotSel, address: f.address || "" }, t);
    orderKeyVal = ""; DLV.slotSel = null;
    cart = {}; save("amura-cart2", cart); renderCartCount(); render();
  }catch(ex){ err.textContent = (ex.message || "Ошибка") + ". Попробуйте ещё раз."; }
  finally{ busy = false; if(btn.isConnected){ btn.disabled = false; btn.textContent = "Заказать"; } }
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
  if(want && AUTH.token && p && p.name){ try{ sessionStorage.removeItem(WANT); }catch{} toast("Вы вошли — оформляем заказ"); go("cart"); }
};

renderCart = renderRetailCart;
dlvRefresh = () => { paint(); const n = $("#rsNote"); if(n) n.textContent = courierNote(); };
document.addEventListener("keydown", e => { if(e.key === "Escape" && $("#rSheet") && !$("#rSheet").hidden) closeSheet(); });
markRetail();
if(VIEW === "cart") renderCart();
})();
