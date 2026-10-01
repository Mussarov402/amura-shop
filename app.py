"""Отдельный сервис сайта AMURA на Render (не трогает amura-price-hook).
Запуск на Render: gunicorn app:app --workers 1 --threads 8 --timeout 60
(один воркер — коды SMS и вход через Telegram хранятся в памяти процесса)."""
from flask import Flask, jsonify
from order_hook import bp

app = Flask(__name__)
app.register_blueprint(bp)


@app.get("/")
def health():
    return jsonify(ok=True, service="amura-shop-api")


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
