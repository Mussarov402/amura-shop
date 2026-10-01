"""Настройки gunicorn — подхватываются автоматически (файл в корне репо).
Команда запуска на Render задаёт --threads 8; здесь число потоков поднимается до GUNICORN_THREADS (по умолчанию 128):
при наплыве заказов потоки ждут МойСклад, и без запаса посетители ждали бы даже каталог.
Процесс по-прежнему один — коды SMS и вход через Telegram хранятся в памяти."""
import os


def on_starting(server):
    server.cfg.set("threads", int(os.environ.get("GUNICORN_THREADS", "128")))
    server.cfg.set("timeout", 120)
