"""Отдельный сервис сайта AMURA на Render (не трогает amura-price-hook).
Запуск на Render: gunicorn app:app --workers 1 --threads 8 --timeout 60
(один воркер — коды SMS и вход через Telegram хранятся в памяти процесса)."""
import traceback

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from order_hook import alert, bp

app = Flask(__name__)
app.register_blueprint(bp)


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


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
