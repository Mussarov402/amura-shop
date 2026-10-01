# AMURA — сайт оптового каталога

- `docs/` — сайт (GitHub Pages: Settings → Pages → main / docs). Адрес: https://mussarov402.github.io/amura-shop/
- `scripts/export_catalog.py` + `.github/workflows/catalog.yml` — выгрузка каталога и фото из МойСклад в `docs/` (8:47, 12:47, 17:47 по Алматы). Секрет репо: `MS_TOKEN`.
- `api/` — сервер сайта (Render, сервис amura-shop-api, Root Directory `api`): живые остатки, заказы в МойСклад, PDF, Telegram-бот, вход в «Я».

Токенов и паролей в репозитории нет — только в Secrets GitHub и Environment Render.
