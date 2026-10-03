"""Отдельный сервис сайта AMURA на Render (не трогает amura-price-hook).
Запуск на Render: gunicorn app:app --workers 1 --threads 8 --timeout 60
(один воркер — коды SMS и вход через Telegram хранятся в памяти процесса)."""
import traceback

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from admin import bp as admin_bp
from order_hook import alert, bp

app = Flask(__name__)
app.register_blueprint(bp)
app.register_blueprint(admin_bp)
from wa import bp as wa_bp  # noqa: E402
app.register_blueprint(wa_bp)


@app.get("/")
def health():
    return jsonify(ok=True, service="amura-shop-api")


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e
    tb = traceback.format_exc()
    print(tb, flush=True)
    alert("err:" + request.path, f"ошибка на сервере\n{request.method} {request.path}\n{e.__class__.__name__}: {str(e)[:300]}\n\n{tb[-700:]}")
    return jsonify(ok=False, error="Что-то пошло не так, попробуйте ещё раз через минуту"), 500


def _watchdog():
    """Самопроверка вместо Health Check в настройках Render: раз в 30 с сервер запрашивает сам себя.
    3 неудачи подряд (всё зависло) — оповещение владельцу и выход процесса; gunicorn сразу поднимает новый."""
    import os
    import threading
    import time
    import urllib.request

    port = os.environ.get("PORT")
    if not port:
        return

    def loop():
        time.sleep(60)
        fails = 0
        while True:
            try:
                ok = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=20).status == 200
            except Exception:
                ok = False
            fails = 0 if ok else fails + 1
            if fails >= 3:
                alert("watchdog", "сервер перестал отвечать — перезапускаю", every=0)
                os._exit(1)
            time.sleep(30)

    threading.Thread(target=loop, daemon=True).start()


_watchdog()


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
