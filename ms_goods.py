"""Товары МойСклад для карточек WB / Ozon (Менеджер WB/Ozon, задача владельца 05.11.2026: «по брендам, которые уже есть
на маркетах, создай остальные позиции, даже не в наличии»). Таких товаров нет в выгрузке сайта (там только товары в наличии).

- products(): код, название, описание, штрихкоды, себестоимость (то же правило, что в калькуляторе: своя → приёмка →
  карточка МС, меньше 100 без валюты — доллары по МиГ), вес, объём, ШВГ, страна, группа и ссылки на фото.
  Чтение для агента: GET /agent/api/read/ms/products (agent.py, тот же ключ и лимит, что у остальных чтений).
- /pub/msimg/<код>/<n>.jpg — публичная ссылка на n-е фото товара из МойСклад: WB и Ozon сами скачивают фото по https.
  Отдаёт только фото товаров (по коду неархивного товара), JPEG, кэш на диске 7 дней; ничего другого из МойСклад не открывает.
"""
import io
import os
import re
import tempfile
import threading
import time

from flask import Blueprint, Response

import order_hook as oh

bp = Blueprint("ms_goods", __name__)
BASE = (os.environ.get("ADMIN_PUBLIC_URL") or "https://admin.amura.kz").rstrip("/")
MAX_CODES, MAX_FOLDER = 50, 50                        # за один запрос: фото — отдельный запрос МС на товар
CODE_RE = re.compile(r"[0-9A-Za-z._-]{1,40}")
CACHE_DIR = os.environ.get("MSIMG_CACHE") or os.path.join(tempfile.gettempdir(), "amura-msimg")
IMG_TTL, IMG_FILES, IMG_MAX_SIDE = 7 * 86400, 2000, 4000
MISS_PER_MIN = 120                                    # скачиваний из МС в минуту — защита от перебора кодов
_imgs, _codes, _miss = {}, {}, []
_lock = threading.Lock()


def _bars(p):
    return [{"type": k, "value": str(v)} for b in p.get("barcodes") or [] for k, v in b.items() if v]


def _item(p, photos=True):
    import delivery
    import mp_calc
    oh._cache[f"mpc_prod:{p['id']}"] = (time.time(), p)          # калькулятор возьмёт эту строку, без нового запроса
    c = mp_calc.product(p["id"])
    at = {a.get("name"): a.get("value") for a in p.get("attributes") or []}
    out = {"id": p["id"], "code": p.get("code") or "", "article": p.get("article") or "", "name": p.get("name") or "",
           "description": p.get("description") or "", "group": p.get("pathName") or "", "barcodes": _bars(p),
           "cost": c["cost"], "costNote": c["costNote"], "buy": c["buy"], "buyCur": c["buyCur"],
           "weight_kg": p.get("weight") or None, "volume_l": p.get("volume") or None,
           **{k: at.get(n) for k, n in zip(("w_cm", "h_cm", "d_cm"), delivery.DIM_ATTRS)},
           "country": ((p.get("country") or {}).get("name") or "") if isinstance(p.get("country"), dict) else ""}
    if photos:
        try:
            out["photos"] = [f"{BASE}/pub/msimg/{out['code']}/{n}.jpg" for n in range(1, len(images(p["id"])) + 1)] if out["code"] else []
        except Exception as e:
            out["photos"], out["photosError"] = [], str(e)[:150]
    return out


def products(codes=None, folder=None, offset=0, photos=True):
    """codes — до 50 кодов (или артикулов) МС; folder — часть названия группы МС (бренд), постранично по 50 (offset).
    → (товары, не найденные коды, следующий offset или None). Архивные не отдаются."""
    exp = {"expand": "buyPrice.currency,country", "limit": 100}
    rows, missing, nxt = [], [], None
    if codes:
        want = [str(c).strip() for c in codes if str(c or "").strip()][:MAX_CODES]
        seen = {}
        for k in range(0, len(want), 40):
            part = want[k:k + 40]
            for field in ("code", "article"):
                for r in oh.ms("GET", "/entity/product", params={**exp, "filter": ";".join(f"{field}={c}" for c in part)},
                               timeout=20).get("rows") or []:
                    if not r.get("archived"):
                        seen.setdefault(r["id"], r)
        rows = list(seen.values())
        got = {str(x).strip().lower() for r in rows for x in (r.get("code"), r.get("article")) if x}
        missing = [c for c in want if c.lower() not in got]
    elif folder:
        offset = max(0, int(offset or 0))
        j = oh.ms("GET", "/entity/product", params={**exp, "limit": MAX_FOLDER, "offset": offset,
                                                     "filter": f"pathName~{folder};archived=false"}, timeout=20)
        rows = j.get("rows") or []
        size = ((j.get("meta") or {}).get("size")) or 0
        nxt = offset + MAX_FOLDER if offset + MAX_FOLDER < size else None
    return [_item(r, photos) for r in rows], missing, nxt


# ---------- фото ----------
def images(pid):
    """Фото товара в МС: [{id, href}] в порядке МойСклад, кэш 10 минут."""
    v = _imgs.get(pid)
    if v and time.time() - v[0] < 600:
        return v[1]
    rows = oh.ms("GET", f"/entity/product/{pid}/images", params={"limit": 100}, timeout=20).get("rows") or []
    out = [{"id": (x.get("meta") or {}).get("href", "").rsplit("/", 1)[-1], "href": (x.get("meta") or {}).get("downloadHref") or ""}
           for x in rows]
    _imgs[pid] = (time.time(), out)
    return out


def _pid(code):
    v = _codes.get(code.lower())
    if v and time.time() - v[0] < (3600 if v[1] else 600):
        return v[1]
    import marketplaces as mpk
    pid = mpk.ms_by_codes([code]).get(code.lower()) or ""
    _codes[code.lower()] = (time.time(), pid)
    return pid


def _jpeg(raw):
    from PIL import Image
    im = Image.open(io.BytesIO(raw))
    if im.mode in ("RGBA", "LA", "P"):                  # прозрачный фон — белым, как требуют WB и Ozon
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    im.thumbnail((IMG_MAX_SIDE, IMG_MAX_SIDE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def _clean():
    try:
        files = sorted((os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)), key=os.path.getmtime)
        for f in files[:max(0, len(files) - IMG_FILES)]:
            os.remove(f)
    except OSError:
        pass


def _allow_miss():
    now = time.time()
    with _lock:
        _miss[:] = [t for t in _miss if now - t < 60]
        if len(_miss) >= MISS_PER_MIN:
            return False
        _miss.append(now)
        return True


@bp.get("/pub/msimg/<code>/<int:n>.jpg")
def msimg_route(code, n):
    if not CODE_RE.fullmatch(code) or not 1 <= n <= 30:
        return "", 404
    try:
        pid = _pid(code)
        if not pid:
            return "", 404
        imgs = images(pid)
    except Exception as e:
        print("Фото МС для площадок:", code, str(e)[:150], flush=True)
        return "", 502
    if n > len(imgs) or not imgs[n - 1]["href"]:
        return "", 404
    path = os.path.join(CACHE_DIR, f"{pid}_{re.sub(r'[^0-9a-f-]', '', imgs[n - 1]['id'])}.jpg")
    data = None
    try:
        if time.time() - os.path.getmtime(path) < IMG_TTL:
            with open(path, "rb") as f:
                data = f.read()
    except OSError:
        pass
    if data is None:
        if not _allow_miss():
            r = Response("", 503)
            r.headers["Retry-After"] = "60"
            return r
        try:
            raw = oh.S.get(imgs[n - 1]["href"], timeout=(10, 60), allow_redirects=True, headers={"Content-Type": None})
            raw.raise_for_status()
            data = _jpeg(raw.content)
        except Exception as e:
            print("Фото МС для площадок:", code, n, e.__class__.__name__, str(e)[:150], flush=True)
            return "", 502
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path + ".tmp", "wb") as f:
                f.write(data)
            os.replace(path + ".tmp", path)
            _clean()
        except OSError as e:
            print("Кэш фото МС:", str(e)[:150], flush=True)
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp
