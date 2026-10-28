"""Отдельный сервис сайта AMURA на Render (не трогает amura-price-hook).
Запуск на Render: gunicorn app:app --workers 1 --threads 8 --timeout 60
(один воркер — коды SMS и вход через Telegram хранятся в памяти процесса)."""
import traceback

from flask import Flask, jsonify, redirect, request
from werkzeug.exceptions import HTTPException

from admin import bp as admin_bp
from order_hook import alert, bp

app = Flask(__name__)
from opt_site import bp as opt_site_bp  # noqa: E402
app.register_blueprint(opt_site_bp)   # opt.amura.kz — оптовый сайт (первым: на этом адресе только сайт)
app.register_blueprint(bp)
app.register_blueprint(admin_bp)
from wa import bp as wa_bp  # noqa: E402
app.register_blueprint(wa_bp)
from ig import bp as ig_bp  # noqa: E402
app.register_blueprint(ig_bp)
from webchat import bp as webchat_bp  # noqa: E402
app.register_blueprint(webchat_bp)
from groups import bp as groups_bp  # noqa: E402
app.register_blueprint(groups_bp)
from checkout import bp as checkout_bp  # noqa: E402
app.register_blueprint(checkout_bp)
from reviews import bp as reviews_bp  # noqa: E402
app.register_blueprint(reviews_bp)
from freedom import bp as freedom_bp  # noqa: E402
app.register_blueprint(freedom_bp)
import freedom  # noqa: E402
freedom.start()                   # оплаты картой, не записанные в МойСклад, — повтор в фоне
from apipay import bp as apipay_bp  # noqa: E402
app.register_blueprint(apipay_bp)
import apipay  # noqa: E402
apipay.start()                    # оплата Kaspi (ApiPay): сверка неоплаченных счетов раз в минуту
from marketplaces import bp as marketplaces_bp  # noqa: E402
app.register_blueprint(marketplaces_bp)
import marketplaces  # noqa: E402
marketplaces.start()              # каналы продаж: заказы Ozon/WB → МойСклад, остатки → маркетплейсы
from sources import bp as sources_bp  # noqa: E402
app.register_blueprint(sources_bp)
from wishes import bp as wishes_bp  # noqa: E402
app.register_blueprint(wishes_bp)
from catalog_status import bp as catalog_status_bp  # noqa: E402
app.register_blueprint(catalog_status_bp)
from partners import bp as partners_bp  # noqa: E402
app.register_blueprint(partners_bp)
from hits import bp as hits_bp  # noqa: E402
app.register_blueprint(hits_bp)
from analytics import bp as analytics_bp  # noqa: E402
app.register_blueprint(analytics_bp)
from retail_stats import bp as retail_stats_bp  # noqa: E402
app.register_blueprint(retail_stats_bp)
from courier import bp as courier_bp  # noqa: E402
app.register_blueprint(courier_bp)
import courier  # noqa: E402
courier.start_poller()            # статусы курьера Яндекса — в фоне, для «Моих заказов» на сайте


SLOW_SEC = 3.0                     # запросы системы AMURA дольше этого — в лог «SLOW <путь> <мс>» (ищем, что тормозит)


@app.before_request
def _t0():
    from flask import g
    import time
    g.t0 = time.time()


@app.after_request
def _slow(resp):
    from flask import g
    import time
    t0 = g.get("t0")
    if t0 and request.path.startswith("/admin/api/"):
        ms = (time.time() - t0) * 1000
        if ms >= SLOW_SEC * 1000:
            print(f"SLOW {request.method} {request.path} {ms:.0f} мс ({resp.status_code})", flush=True)
    return resp


@app.get("/")
def health():
    if request.host.lower().startswith("admin."):   # admin.amura.kz открывает панель сразу
        return redirect("/admin", 302)
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


def rss_mb():
    """Сколько памяти занимает процесс сейчас (МБ), 0 — если не узнать (не Linux)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def mem_step(limit, trim=None, log=print):
    """Один шаг memguard: вернуть системе свободную память и решить, пора ли перезапуск. True — выше limit МБ."""
    if trim:
        try:
            trim(0)
        except Exception:
            pass
    rss = rss_mb()
    log(f"MEM rss={rss} МБ, потоков {__import__('threading').active_count()}", flush=True)
    return rss > limit


def _memguard():
    """Раз в 5 минут свободная память возвращается системе (malloc_trim). Если процесс всё равно больше MEM_LIMIT_MB
    (по умолчанию 430 из 512 МБ на Render) — плавный перезапуск воркера (SIGTERM: текущие запросы дорабатывают,
    gunicorn сразу поднимает новый), а не SIGKILL от Render посреди запроса."""
    import ctypes
    import os
    import signal
    import threading
    import time

    if not os.environ.get("PORT"):
        return
    limit = int(os.environ.get("MEM_LIMIT_MB", "430"))
    try:
        trim = ctypes.CDLL("libc.so.6").malloc_trim
    except (OSError, AttributeError):
        trim = None

    def loop():
        n = 0
        while True:
            time.sleep(300)
            n += 1
            if mem_step(limit, trim, log=print if n % 12 == 0 else (lambda *a, **k: None)):
                print(f"MEM выше {limit} МБ — плавный перезапуск воркера", flush=True)
                os.kill(os.getpid(), signal.SIGTERM)
                return

    threading.Thread(target=loop, daemon=True).start()


_memguard()

if __import__("os").environ.get("PORT"):          # только на сервере: разделы-дубли МойСклад один раз выключаются (17.10.2026)
    import modules  # noqa: E402
    modules.simplify_1017()
    import threading  # noqa: E402
    import admin as _admin  # noqa: E402
    threading.Thread(target=_admin.warm, daemon=True).start()   # первые нажатия после обновления не ждут МойСклад


if __name__ == "__main__":
    import os
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
