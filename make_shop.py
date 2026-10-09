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
    r = legal_req()
    if legal_ready(r):                            # ссылки внизу и согласие у «Заказать» — только когда заполнены реквизиты
        s = rep(s, "</body>", legal_footer(r) + "<script>window.AMURA_LEGAL = true;</script>\n</body>")
    s = rep(s, "</head>", "<style>\n" + css + "</style>\n</head>")
    s = rep(s, "</body>", "<script>\n" + js + "</script>\n</body>")
    return s


# юридические страницы для банка (Halyk ePay): оферта, политика, доставка и оплата, возврат, контакты — retail/legal/
LEGAL = [("offer", "Оферта"), ("privacy", "Конфиденциальность"), ("delivery", "Доставка и оплата"), ("returns", "Возврат и обмен"),
         ("contacts", "Контакты")]
LEGAL_NEED = ("seller", "iin", "address", "phone", "email")          # без них ссылки на сайте не показываем


def legal_req():
    with open(os.path.join(ROOT, "retail", "legal", "requisites.json"), encoding="utf-8") as f:
        return json.load(f)


def legal_ready(r):
    return all(str(r.get(k, "")).strip() for k in LEGAL_NEED)


def legal_pages(r):
    import html
    e = lambda k: html.escape(str(r.get(k, "")).strip() or "—")  # noqa: E731
    req = (f'<div class="req"><b>{e("seller")}</b><br>ИИН: {e("iin")}<br>Адрес: {e("address")}<br>'
           f'Телефон: {e("phone")}<br>E-mail: {e("email")}<br>Сайт: {e("site")}</div>')
    nav = " · ".join(f'<a href="{k}.html">{t}</a>' for k, t in LEGAL)
    out = {}
    for k, title in LEGAL:
        with open(os.path.join(ROOT, "retail", "legal", k + ".html"), encoding="utf-8") as f:
            body = f.read().replace("{{requisites}}", req)
        for key in r:
            body = body.replace("{{" + key + "}}", e(key))
        out[k] = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — AMURA</title><meta name="robots" content="noindex,follow">
<style>body{{margin:0;font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#16201B;background:#F5F6F4}}
.w{{max-width:760px;margin:0 auto;padding:20px 16px 48px}}a{{color:#0E3B2C}}h1{{font-size:26px;line-height:1.25;margin:14px 0 6px}}
h2{{font-size:18px;margin:26px 0 6px}}.meta{{color:#5F6B65;font-size:14px;margin:0 0 14px}}.top{{font-weight:700;text-decoration:none}}
.req{{background:#fff;border:1px solid #DDE3DF;border-radius:12px;padding:12px 14px}}.nav{{margin-top:36px;font-size:14px;color:#5F6B65}}
ul{{padding-left:20px}}li{{margin:6px 0}}</style></head>
<body><div class="w"><a class="top" href="../">← AMURA, в магазин</a>
{body}
<div class="nav">{nav}</div></div></body></html>
"""
    return out


def legal_footer(r):
    links = "".join(f'<a href="legal/{k}.html">{t}</a>' for k, t in LEGAL)
    return (f'<footer class="rlegal"><nav>{links}</nav><div>{r.get("seller", "")} · ИИН {r.get("iin", "")} · '
            f'Оплата картами Visa и Mastercard</div></footer>\n')


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
    os.makedirs(os.path.join(ROOT, "shop", "legal"), exist_ok=True)
    for k, page in legal_pages(legal_req()).items():
        with open(os.path.join(ROOT, "shop", "legal", k + ".html"), "w", encoding="utf-8") as f:
            f.write(page)
    with open(os.path.join(ROOT, "shop", "manifest.webmanifest"), "w", encoding="utf-8") as f:
        json.dump(MANIFEST, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
