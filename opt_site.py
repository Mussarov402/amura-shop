"""Оптовый сайт на opt.amura.kz — тот же оптовый index.html, что раньше был на amura.kz.

Сервер (этот же сервис на Render) берёт файлы сайта с GitHub Pages (amura.kz) и отдаёт их на opt.amura.kz,
поэтому каталог, остатки и фото обновляются сразу, без деплоя. Страница и её манифест — из папки /opt/,
а пока её нет (до переключения amura.kz на розницу) — из корня. Фото — переадресацией на amura.kz/img/.
"""
import os
import threading
import time

import requests
from flask import Blueprint, Response, redirect, request

bp = Blueprint("opt_site", __name__)

ORIGIN = os.environ.get("OPT_SITE_ORIGIN", "https://amura.kz").rstrip("/")
OWN = {"", "index.html", "manifest.webmanifest"}   # свои файлы оптового сайта лежат в /opt/
PASS = ("content-type", "last-modified", "etag")
_cache = {}                                          # путь -> (время, код, заголовки, тело)
_lock = threading.Lock()


def is_opt_host():
    return request.host.split(":")[0].lower().startswith("opt.")


def _ttl(path):
    return 3600 if path.startswith(("fonts/", "vendor/")) or path.endswith(".png") else 60


def _get(url):
    r = requests.get(url, timeout=15, headers={"User-Agent": "amura-opt-site/1.0"})
    return r.status_code, {k: r.headers[k] for k in r.headers if k.lower() in PASS}, r.content


def fetch(path):
    now = time.time()
    with _lock:
        hit = _cache.get(path)
    if hit and now - hit[0] < _ttl(path):
        return hit[1:]
    try:
        res = None
        if path in OWN:
            res = _get(f"{ORIGIN}/opt/{path}")
            if res[0] == 404:
                res = None
        if res is None:
            res = _get(f"{ORIGIN}/{path}")
    except requests.RequestException:
        if hit:                                      # GitHub не ответил — отдаём то, что было
            return hit[1:]
        return 502, {"Content-Type": "text/plain; charset=utf-8"}, "Сайт временно недоступен, обновите страницу".encode()
    if res[0] == 200:
        with _lock:
            if len(_cache) > 300:
                _cache.clear()
            _cache[path] = (now, *res)
    elif hit:
        return hit[1:]
    return res


@bp.before_app_request
def serve():
    if not is_opt_host():
        return None
    path = request.path.lstrip("/")
    if request.method not in ("GET", "HEAD"):
        return Response("Method Not Allowed", 405)
    if path.startswith("img/"):                      # фото — напрямую с amura.kz (GitHub Pages)
        return redirect(f"{ORIGIN}/{path}", 302)
    if path == "opt" or path.startswith("opt/") or ".." in path:
        return redirect("/", 302)
    code, headers, body = fetch(path)
    resp = Response(body, status=code)
    for k, v in headers.items():
        resp.headers[k] = v
    resp.headers["Cache-Control"] = "no-cache" if _ttl(path) <= 60 else "public, max-age=3600"
    return resp
