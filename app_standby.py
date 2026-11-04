"""Приложение AMURA на запасном сервере (Hoster.kz) до переключения: gunicorn app_standby:app.
AMURA_STANDBY=1 — фоновые потоки не запускаются (ни при импорте модулей, ни в воркере): сервер не пишет в МойСклад,
не забирает заказы маркетплейсов и не шлёт уведомления параллельно с основным сервером на Render.
Исходящие сообщения от имени системы AMURA тоже выключены: Telegram (тревоги alert, уведомления персоналу) и push —
ошибки копии не должны приходить владельцу как будто с основного сервера; они пишутся только в журнал сервера."""
import bg

with bg.standby_import():
    from app import app  # noqa: E402,F401

import order_hook as _oh  # noqa: E402
import push as _push  # noqa: E402

_oh.BOT = ""                                                     # tg() без токена ничего не отправляет
_push.notify = lambda *a, **k: print("Ожидание: push не отправлен —", (a or [""])[0], flush=True)
