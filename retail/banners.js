/* ===== Розница: свои баннеры (система AMURA → Товары → Баннеры → «Розничный сайт») ===== */
// Размеры, картинка для телефона (imgM) и кэш прошлого визита — общие с оптом (renderBanners / loadBanners в opt/index.html).
const RBANNERS_DEFAULT = { autoplaySec: 6, slides: [
  { title: "Корейская косметика с доставкой", text: "Оригинальный уход со склада в Алматы. Доставка по городу и по всему Казахстану.", tags: ["Оригинал", "Доставка по Казахстану"], bg: "#14503C" },
  { title: "Новинки недели", text: "Свежие поступления — смотрите первыми.", button: "Смотреть новинки", link: { sort: "new" }, bg: "#1E3A5F" }
] };
loadBanners(CONFIG.apiUrl + "/banners?site=retail", RBANNERS_DEFAULT).catch(() => {});
