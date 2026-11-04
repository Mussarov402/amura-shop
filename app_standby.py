"""Приложение AMURA на запасном сервере (Hoster.kz) до переключения: gunicorn app_standby:app.
AMURA_STANDBY=1 — фоновые потоки не запускаются (ни при импорте модулей, ни в воркере): сервер не пишет в МойСклад,
не забирает заказы маркетплейсов и не шлёт уведомления параллельно с основным сервером на Render."""
import bg

with bg.standby_import():
    from app import app  # noqa: E402,F401
