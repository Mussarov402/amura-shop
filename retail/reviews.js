/* Розничный сайт: отзывы о товарах. Звёзды на карточках, список отзывов и «Оставить отзыв» в окне товара.
   Оставить отзыв может только вошедший покупатель; новые отзывы появляются после проверки в панели (Товары → Отзывы). */
(function(){
if(!window.AMURA_RETAIL) return;
let ON = true;
const STAR = n => `<span class="rv-st">${"★".repeat(n)}<i>${"★".repeat(5 - n)}</i></span>`;
const plur = n => { const m = n % 10, h = n % 100; return m === 1 && h !== 11 ? "отзыв" : m >= 2 && m <= 4 && (h < 10 || h >= 20) ? "отзыва" : "отзывов"; };

async function loadSummary(){
  if(DEMO) return;
  try{
    const j = await getJSON(`${CONFIG.apiUrl}/reviews/summary`, 15000);
    ON = j.on !== false; window.RV_SUM = j.items || {};
    if(ALL.length){ render(); if(VIEW === "fav") renderFav(); }
  }catch{}
}

function boxHTML(j, it){
  const mine = j.mine;
  return `<div class="rv-h"><h4>Отзывы</h4>${j.count ? `<span>${STAR(Math.round(j.avg))} <b>${String(j.avg).replace(".", ",")}</b> · ${fmt(j.count)} ${plur(j.count)}</span>` : ""}</div>
    ${j.items.length ? j.items.map(r => `<div class="rv-i">
      <div class="rv-t">${STAR(r.rating)}<span class="kv">${esc(r.date)}</span></div>
      <div class="rv-n">${esc(r.name)}${r.city ? `, ${esc(r.city)}` : ""}${r.verified ? ' <em>купил на сайте</em>' : ""}</div>
      ${r.text ? `<p>${esc(r.text)}</p>` : ""}
      ${r.answer ? `<div class="rv-a"><b>Ответ AMURA:</b> ${esc(r.answer)}</div>` : ""}</div>`).join("")
      : `<p class="kv">Отзывов пока нет — будьте первым.</p>`}
    ${mine && mine.status === "new" ? `<p class="kv">Ваш отзыв на проверке — скоро появится.</p>` : ""}
    <button type="button" class="btn ghost" data-rvw>${!AUTH.token ? "Войти и оставить отзыв" : mine ? "Изменить мой отзыв" : "Оставить отзыв"}</button>
    <div id="rvForm"></div>`;
}
function formHTML(mine){
  const r = mine ? mine.rating : 0;
  return `<div class="rv-f"><div class="rv-pick" role="radiogroup" aria-label="Оценка">${[1, 2, 3, 4, 5].map(n => `<button type="button" data-rvs="${n}" class="${n <= r ? "on" : ""}" aria-label="${n} из 5">★</button>`).join("")}</div>
    <textarea id="rvText" rows="4" maxlength="2000" placeholder="Что понравилось, что нет, как подошло">${esc(mine ? mine.text : "")}</textarea>
    <div class="err" id="rvErr"></div><button type="button" class="btn" data-rvsend>Отправить отзыв</button></div>`;
}
async function loadBox(it){
  const box = $("#rvBox"); if(!box) return;
  try{
    const r = await fetch(`${CONFIG.apiUrl}/reviews?id=${encodeURIComponent(it.id)}`, { headers: authHeaders() });
    const j = await r.json();
    if($("#sheet").dataset.id !== it.id) return;
    if(j.on === false){ ON = false; box.remove(); return; }
    box.innerHTML = boxHTML(j, it); box.dataset.mine = JSON.stringify(j.mine || null);
  }catch{ box.innerHTML = `<p class="kv">Отзывы сейчас не загрузились.</p>`; }
}
const _open = openProduct;
openProduct = function(it){
  _open(it);
  if(!ON || DEMO) return;
  const info = $("#sheet .info"); if(!info) return;
  info.insertAdjacentHTML("beforeend", `<div class="rv-box" id="rvBox"><p class="kv">Загружаем отзывы…</p></div>`);
  loadBox(it);
};
$("#sheet").addEventListener("click", async e => {
  const box = $("#rvBox"); if(!box || !box.contains(e.target)) return;
  const it = byId($("#sheet").dataset.id); if(!it) return;
  if(e.target.closest("[data-rvw]")){
    if(!AUTH.token){ closeAll(); toast("Войдите — и сможете оставить отзыв"); return go("me"); }
    $("#rvForm").innerHTML = formHTML(JSON.parse(box.dataset.mine || "null")); e.target.closest("[data-rvw]").hidden = true; return;
  }
  const st = e.target.closest("[data-rvs]");
  if(st){ const n = +st.dataset.rvs; box.querySelectorAll("[data-rvs]").forEach(b => b.classList.toggle("on", +b.dataset.rvs <= n)); box.dataset.rate = n; return; }
  if(e.target.closest("[data-rvsend]")){
    const btn = e.target.closest("[data-rvsend]"), err = $("#rvErr");
    const rating = +(box.dataset.rate || box.querySelectorAll("[data-rvs].on").length), text = $("#rvText").value.trim();
    if(!rating){ err.textContent = "Поставьте оценку — нажмите на звёзды"; return; }
    btn.disabled = true; btn.textContent = "Отправляем…";
    try{
      const r = await fetch(`${CONFIG.apiUrl}/reviews`, { method: "POST", headers: authHeaders(), body: JSON.stringify({ id: it.id, rating, text }) });
      const j = await r.json().catch(() => ({}));
      if(r.status === 401){ closeAll(); toast("Войдите заново"); return go("me"); }
      if(!r.ok || !j.ok) throw new Error(j.error || "Не получилось отправить");
      toast(j.status === "new" ? "Спасибо! Отзыв появится после проверки" : "Спасибо за отзыв!");
      loadBox(it); loadSummary();
    }catch(ex){ err.textContent = ex.message; btn.disabled = false; btn.textContent = "Отправить отзыв"; }
  }
});
loadSummary();
})();
