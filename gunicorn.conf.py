"""Настройки gunicorn — подхватываются автоматически (файл в корне репо).
Команда запуска на Render задаёт --threads 8; здесь число потоков поднимается до GUNICORN_THREADS (по умолчанию 128):
при наплыве заказов потоки ждут МойСклад, и без запаса посетители ждали бы даже каталог.
Процесс по-прежнему один — коды SMS и вход через Telegram хранятся в памяти.

Память (28.10.2026): со 128 потоками glibc заводит каждому активному потоку свою «арену» памяти, освобождённое в ней
системе не возвращается — за день процесс дорастал до 512 МБ и Render убивал его (SIGKILL). В воркере до загрузки
приложения арен не больше MALLOC_ARENAS (по умолчанию 2); остальное — memguard в app.py."""
import ctypes
import os

M_ARENA_MAX = -8


def on_starting(server):
    server.cfg.set("threads", int(os.environ.get("GUNICORN_THREADS", "128")))
    server.cfg.set("timeout", 120)


def post_fork(server, worker):
    try:
        ctypes.CDLL("libc.so.6").mallopt(M_ARENA_MAX, int(os.environ.get("MALLOC_ARENAS", "2")))
    except (OSError, AttributeError, ValueError):
        pass
