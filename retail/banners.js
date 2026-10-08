/* ===== Розница: свои баннеры (панель → Товары → Баннеры → «Розничный сайт») ===== */
// Высота карусели (size: s/m/l) и отдельная картинка для телефона (imgM) — только здесь, оптовый сайт их не знает.
const RBANNERS_DEFAULT = { autoplaySec: 6, size: "m", slides: [
  { title: "Корейская косметика с доставкой", text: "Оригинальный уход со склада в Алматы. Доставка по городу и по всему Казахстану.", tags: ["Оригинал", "Доставка по Казахстану"], bg: "#14503C" },
  { title: "Новинки недели", text: "Свежие поступления — смотрите первыми.", button: "Смотреть новинки", link: { sort: "new" }, bg: "#1E3A5F" }
] };
(function(){
  const base = renderBanners;
  renderBanners = function(cfg){
    base(cfg);
    const box = $("#banners");
    box.dataset.size = ["s", "m", "l"].includes(cfg.size) ? cfg.size : "m";
    const mob = {};
    (cfg.slides || []).forEach(b => { if(b.img && b.imgM) mob[b.img] = b.imgM; });
    box.querySelectorAll(".bSlide.pic img").forEach(img => {
      const m = mob[img.getAttribute("src")];
      if(!m) return;
      const pic = document.createElement("picture"), src = document.createElement("source");
      src.media = "(max-width:640px)"; src.srcset = m;
      img.replaceWith(pic); pic.append(src, img);
      img.closest(".bSlide").classList.add("hasM");
    });
  };
  renderBanners(RBANNERS_DEFAULT);
  getJSON(CONFIG.apiUrl + "/banners?site=retail", 8000).then(c => renderBanners(c)).catch(() => {});
})();
