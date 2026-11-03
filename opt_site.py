"""Сайты на этом сервере (Render): опт — opt.amura.kz, розница — amura.kz (быстрый путь к оценке A на SSL Labs, 03.11.2026).

Опт (слово владельца 03.11.2026: «на опте не надо менять ничего») — как раньше: файлы с https://amura.kz по DNS,
страница и манифест — из /opt/, остальное — из корня, фото — переадресацией на amura.kz/img/, без HSTS. Когда amura.kz
переедет на этот сервер, опт продолжит брать файлы с amura.kz — их отдаст розничная часть ниже.

Розница amura.kz (и www → amura.kz) — корень GitHub Pages как есть (shop/ — переадресации, shop/legal/ — юридические
страницы), с HSTS, фото кэшируются. Источник файлов — по очереди, первый ответивший 200:
1. https://pages.amura.kz — GitHub Pages под своим адресом (после переключения домена Pages на pages.amura.kz);
2. серверы GitHub Pages напрямую под старым именем amura.kz — пока домен Pages ещё amura.kz (работает и до, и после того,
   как DNS amura.kz переведут на Render: соединяемся с IP GitHub, а не по DNS, и проверяем сертификат amura.kz);
3. http://pages.amura.kz — первые минуты после переключения домена Pages, пока GitHub выпускает сертификат;
4. https://amura.kz по DNS — как было раньше (запасной, пока DNS amura.kz смотрит на GitHub).
Источник, который не отвечает, пропускаем 5 минут. GitHub не ответил совсем — отдаём последнюю копию.
"""
import os
import threading
import time

import requests
import urllib3
from flask import Blueprint, Response, redirect, request

bp = Blueprint("opt_site", __name__)

PAGES = os.environ.get("SITE_PAGES_HOST", "pages.amura.kz")
OLD = "amura.kz"                                     # домен GitHub Pages до переключения
GH_IPS = ("185.199.108.153", "185.199.109.153", "185.199.110.153", "185.199.111.153")   # адреса GitHub Pages (документация GitHub)
RTL_HOSTS = {"amura.kz", "www.amura.kz"}
OWN = {"", "index.html", "manifest.webmanifest"}   # свои файлы оптового сайта лежат в /opt/
PASS = ("content-type", "last-modified", "etag", "location")
LOOP = "X-Amura-Site-Proxy"                          # наш собственный запрос — не проксируем сами себя
HSTS = "max-age=31536000"
_cache = {}                                          # (сайт, путь) -> (время, код, заголовки, тело)
_img = {}                                            # фото: путь -> (время, код, заголовки, тело), не больше IMG_MAX байт
_img_size = [0]
IMG_MAX, IMG_ONE = 40 * 1024 * 1024, 400 * 1024
_down = {}                                           # источник -> до какого времени не пробовать
_lock = threading.Lock()


def site():
    h = request.host.split(":")[0].lower()
    if h.startswith("opt."):
        return "opt"
    if h in RTL_HOSTS:
        return "rtl"
    return None


def is_opt_host():
    return site() == "opt"


def _ttl(path):
    if path.startswith("img/"):
        return 86400
    return 3600 if path.startswith(("fonts/", "vendor/")) or path.endswith(".png") else 60


def _pages(path, scheme="https", host=None):
    r = requests.get(f"{scheme}://{host or PAGES}/{path}", timeout=15, allow_redirects=False,
                     headers={"User-Agent": "amura-site/1.0", LOOP: "1"})
    return r.status_code, {k: r.headers[k] for k in r.headers if k.lower() in PASS}, r.content


def _github(path):
    """GitHub Pages напрямую под именем amura.kz (SNI и проверка сертификата — amura.kz), минуя DNS."""
    import certifi
    err = None
    for ip in GH_IPS:
        try:
            pool = urllib3.HTTPSConnectionPool(ip, 443, server_hostname=OLD, assert_hostname=OLD, cert_reqs="CERT_REQUIRED",
                                               ca_certs=certifi.where(), timeout=urllib3.Timeout(connect=5, read=15), retries=False)
            r = pool.request("GET", "/" + path, headers={"Host": OLD, "User-Agent": "amura-site/1.0", LOOP: "1"}, redirect=False)
            return r.status, {k: v for k, v in r.headers.items() if k.lower() in PASS}, r.data
        except Exception as e:                       # следующий адрес GitHub
            err = e
    raise requests.RequestException(str(err))


SOURCES = (("pages", lambda p: _pages(p)), ("github", lambda p: _github(p)), ("pages-http", lambda p: _pages(p, "http")),
           ("old", lambda p: _pages(p, host=OLD)))    # как раньше — amura.kz по DNS (пока DNS на GitHub; после — 508 и пропуск)


OPT_ORIGIN = os.environ.get("OPT_SITE_ORIGIN", "https://amura.kz").rstrip("/")
OPT_PASS = ("content-type", "last-modified", "etag")


def _get_opt(path):
    """Опт — как до 03.11.2026 (слово владельца: на опте ничего не менять): файлы с amura.kz по DNS."""
    r = requests.get(f"{OPT_ORIGIN}/{path}", timeout=15, headers={"User-Agent": "amura-opt-site/1.0"})
    return r.status_code, {k: r.headers[k] for k in r.headers if k.lower() in OPT_PASS}, r.content


def _get(path):
    """Первый источник с ответом 200; иначе — последний полученный ответ (настоящий 404); никто не ответил — исключение."""
    now, last = time.time(), None
    for name, fn in SOURCES:
        if _down.get(name, 0) > now:
            continue
        try:
            res = fn(path)
        except Exception:
            _down[name] = now + 300
            continue
        if res[0] == 200:
            return res
        last = last or res
    if last is None:
        _down.clear()                                # все молчат — в следующий раз пробуем все заново
        raise requests.RequestException("нет источника")
    return last


def fetch(path, kind="opt"):
    now = time.time()
    key = (kind, path)
    store = _img if path.startswith("img/") else _cache
    with _lock:
        hit = store.get(key)
    if hit and now - hit[0] < _ttl(path):
        return hit[1:]
    try:
        res = None
        get = _get_opt if kind == "opt" else _get
        if kind == "opt" and path in OWN:
            res = get(f"opt/{path}")
            if res[0] == 404:
                res = None
        if res is None:
            res = get(path)
    except requests.RequestException:
        if hit:                                      # GitHub не ответил — отдаём то, что было
            return hit[1:]
        return 502, {"Content-Type": "text/plain; charset=utf-8"}, "Сайт временно недоступен, обновите страницу".encode()
    if res[0] == 200:
        with _lock:
            if store is _img:
                if len(res[2]) <= IMG_ONE:
                    if _img_size[0] + len(res[2]) > IMG_MAX:
                        _img.clear()
                        _img_size[0] = 0
                    _img[key] = (now, *res)
                    _img_size[0] += len(res[2])
            else:
                if len(_cache) > 300:
                    _cache.clear()
                _cache[key] = (now, *res)
    elif hit:
        return hit[1:]
    return res


@bp.before_app_request
def serve():
    kind = site()
    if not kind:
        return None
    if kind == "opt":
        return serve_opt()
    if request.headers.get(LOOP):                    # DNS уже на нас, а источник указывает сюда же — не зацикливаемся
        return Response("loop", 508)
    if kind == "rtl" and request.host.split(":")[0].lower() != "amura.kz":
        return redirect("https://amura.kz" + request.full_path.rstrip("?"), 301)
    path = request.path.lstrip("/")
    if request.method not in ("GET", "HEAD"):
        return Response("Method Not Allowed", 405)
    if ".." in path:
        return redirect("/", 302)
    code, headers, body = fetch(path, kind)
    resp = Response(body, status=code)
    for k, v in headers.items():
        if k.lower() == "location":                  # переадресация GitHub (shop → shop/) — на наш же адрес
            for h in (f"https://{PAGES}", f"http://{PAGES}", f"https://{OLD}", f"http://{OLD}"):
                if v.startswith(h + "/") or v == h:
                    v = v[len(h):] or "/"
        resp.headers[k] = v
    t = _ttl(path)
    resp.headers["Cache-Control"] = "no-cache" if t <= 60 else f"public, max-age={t}"
    return resp


def serve_opt():
    """opt.amura.kz — ровно как до 03.11.2026: файлы с amura.kz, фото — переадресацией туда же, без HSTS."""
    path = request.path.lstrip("/")
    if request.method not in ("GET", "HEAD"):
        return Response("Method Not Allowed", 405)
    if path.startswith("img/"):
        return redirect(f"{OPT_ORIGIN}/{path}", 302)
    if path == "opt" or path.startswith("opt/") or ".." in path:
        return redirect("/", 302)
    code, headers, body = fetch(path, "opt")
    resp = Response(body, status=code)
    for k, v in headers.items():
        resp.headers[k] = v
    resp.headers["Cache-Control"] = "no-cache" if _ttl(path) <= 60 else "public, max-age=3600"
    return resp


@bp.after_app_request
def hsts(resp):
    if site() == "rtl":                              # только amura.kz; опт — без изменений
        resp.headers.setdefault("Strict-Transport-Security", HSTS)
    return resp
