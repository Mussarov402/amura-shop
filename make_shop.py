"""Розничный сайт amura.kz/shop — тот же index.html в режиме розницы (window.AMURA_RETAIL).

Запуск: python make_shop.py  → пересобирает shop/index.html и shop/manifest.webmanifest.
Отдельно shop/index.html не редактировать: правки — в index.html (общее) или retail/ (только розница), потом этот скрипт
(GitHub Action «Розничный сайт» делает это сам при каждом изменении index.html в main).
"""
import json
import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def build(src):
    def rep(s, old, new):
        if old not in s:
            raise SystemExit(f"make_shop: в index.html не найдено: {old}")
        return s.replace(old, new, 1)
    s = rep(src, "<head>\n", "<head>\n<!-- Собрано из index.html скриптом make_shop.py — не редактировать вручную -->\n"
            "<script>window.AMURA_RETAIL = true; document.documentElement.dataset.theme = 'light';</script>\n")   # розница всегда светлая, как WB
    s = rep(s, "<title>AMURA — оптовый каталог</title>", "<title>AMURA — корейская косметика</title>")
    s = rep(s, '<meta name="description" content="Оригинальная корейская косметика оптом. Цены поштучно, от 10 штук и от короба.">',
            '<meta name="description" content="Оригинальная корейская косметика с доставкой по Алматы и Казахстану.">')
    s = rep(s, '<link rel="manifest" href="manifest.webmanifest">', '<link rel="manifest" href="/shop/manifest.webmanifest">')
    s = rep(s, '<link rel="apple-touch-icon" href="icon-180.png">', '<link rel="apple-touch-icon" href="/icon-180.png">')
    # оформление заказа как на WB — только для розницы, оптовый index.html не меняется
    def read(name):
        with open(os.path.join(ROOT, "retail", name), encoding="utf-8") as f:
            return f.read()
    # свои баннеры розницы (retail/banners.js): оптовые не загружаем
    s = rep(s, "renderBanners(BANNERS_DEFAULT);\n", "")
    s = rep(s, 'getJSON(CONFIG.apiUrl + "/banners", 8000).catch(() => getJSON("banners.json", 8000)).then(renderBanners).catch(() => {});',
            "/* баннеры розницы — retail/banners.js */")
    css = read("cards.css") + read("cart.css") + read("reviews.css") + read("banners.css") + read("product.css")    # карточки, корзина как на WB, отзывы, баннеры, страница товара
    js = read("cards.js") + read("cart.js") + read("reviews.js") + read("banners.js") + read("product.js")
    s = rep(s, "</head>", "<style>\n" + css + "</style>\n</head>")
    s = rep(s, "</body>", "<script>\n" + js + "</script>\n</body>")
    return s


MANIFEST = {
    "name": "AMURA — корейская косметика", "short_name": "AMURA", "start_url": "/shop/", "scope": "/shop/",
    "display": "standalone", "background_color": "#F5F6F4", "theme_color": "#0E3B2C", "lang": "ru",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
}


def main():
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        out = build(f.read())
    os.makedirs(os.path.join(ROOT, "shop"), exist_ok=True)
    with open(os.path.join(ROOT, "shop", "index.html"), "w", encoding="utf-8") as f:
        f.write(out)
    with open(os.path.join(ROOT, "shop", "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump(MANIFEST, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
