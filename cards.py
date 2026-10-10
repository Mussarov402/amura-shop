"""Карточки товаров в системе AMURA (модуль «Карточки товаров», docs/MIGRATION.md).

Фото — в МойСклад (он главный; на сайт фото попадают как раньше — часовой выгрузкой export_catalog.py):
- загруженное фото сначала сохраняется у нас (card_photo_q), затем отправляется в МойСклад (/entity/product/<id>/images);
- имя файла уникальное (amura-<id операции>.<расширение>) — перед повтором ищем его среди фото товара, второй копии не будет;
- сбой сети/5xx — очередь и фоновый повтор, отказ МойСклад (4xx) — «Ошибка» и кнопка «Повторить»;
- отправленное фото из нашей базы удаляется (хранит его МойСклад).
Видео — одно на товар, только у нас (МойСклад видео не хранит): сжимается в фоне до 720p H.264 и лежит в product_video;
отдаётся по /media/product/<id>.mp4 с поддержкой перемотки (Range). Сайт его пока не показывает."""
import base64
import json
import os
import tempfile
import threading
import time
import uuid

import inbox
import mirror
import modules
import order_hook as oh

PHOTO_MAX = 10 * 1024 * 1024          # фото до 10 МБ
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MS_PHOTOS_MAX = 10                    # МойСклад хранит до 10 фото на товар
VIDEO_MAX = 200 * 1024 * 1024         # исходник до 200 МБ (после сжатия — обычно 3–15 МБ)
VIDEO_OUT_MAX = 40 * 1024 * 1024
VIDEO_SECONDS = 120                   # длиннее — обрезается до 2 минут
RETRY_EVERY = 60
MAX_AUTO = 30
_ready = [False]
_lock = threading.Lock()
_vlock = threading.Semaphore(1)       # одно сжатие видео за раз — не отнимаем процессор у сайта
_imgs = {}                            # pid -> (время, [фото]) — список фото товара из МойСклад, 2 минуты
_bytes = {}                           # (pid, iid, size) -> (mime, байты) — уменьшенные фото для показа


def _schema(d):
    if _ready[0]:
        return
    blob = "BYTEA" if inbox.PG else "BLOB"
    d.run("CREATE TABLE IF NOT EXISTS card_photo_q (id TEXT PRIMARY KEY, pid TEXT, filename TEXT, mime TEXT, data " + blob + ","
          " status TEXT, attempts INTEGER DEFAULT 0, error TEXT, who TEXT, created DOUBLE PRECISION, sent DOUBLE PRECISION,"
          " next_try DOUBLE PRECISION DEFAULT 0)")
    d.run("CREATE TABLE IF NOT EXISTS product_video (pid TEXT PRIMARY KEY, mime TEXT, data " + blob + ", size INTEGER,"
          " status TEXT, error TEXT, name TEXT, who TEXT, at DOUBLE PRECISION)")
    d.c.commit()
    _ready[0] = True


def db():
    d = inbox.db()
    _schema(d)
    return d


def enabled():
    return modules.enabled("cards")


# ---------- карточка ----------
def card(pid):
    """Данные товара из зеркала (быстро, без МойСклад) + видео + очередь фото."""
    with mirror.db() as d:
        r = d.run("SELECT id, kind, code, article, name, folder, prices, buy_price, barcodes, archived FROM ms_product WHERE id=%s AND deleted=0",
                  (pid,), one=True)
        if not r:
            return None
        names = dict(d.run("SELECT id, name FROM ms_store", many=True) or [])
        stock = d.run("SELECT store_id, stock, reserve FROM ms_stock WHERE product_id=%s", (pid,), many=True) or []
    i, kind, code, art, name, folder, prices, buy, bcs, arch = r
    return {"id": i, "kind": kind or "product", "code": code or "", "article": art or "", "name": name or "", "folder": folder or "",
            "prices": json.loads(prices or "{}"), "buy": round(buy or 0), "barcodes": json.loads(bcs or "[]"), "archived": bool(arch),
            "stock": sorted([{"store": names.get(s, s), "stock": a or 0, "reserve": b or 0} for s, a, b in stock if a or b], key=lambda x: x["store"]),
            "site": f"{oh.SITE_URL.rstrip('/')}/img/{i}.webp" if getattr(oh, "SITE_URL", "") else "",
            "video": video_info(i), "pending": pending(i)}


# ---------- фото ----------
def _ms_images(pid, fresh=False):
    c = _imgs.get(pid)
    if c and not fresh and time.time() - c[0] < 120:
        return c[1]
    rows = oh.ms("GET", f"/entity/product/{pid}/images", params={"limit": 100}, timeout=30).get("rows") or []
    out = [{"id": (x.get("meta") or {}).get("href", "").rsplit("/", 1)[-1], "filename": x.get("filename") or "",
            "full": (x.get("meta") or {}).get("downloadHref") or "", "mini": (x.get("miniature") or {}).get("downloadHref") or (x.get("miniature") or {}).get("href") or "",
            "tiny": (x.get("tiny") or {}).get("href") or ""} for x in rows]
    _imgs[pid] = (time.time(), out)
    return out


def photos(pid):
    return [{"id": x["id"], "filename": x["filename"]} for x in _ms_images(pid)]


def photo_bytes(pid, iid, size="mini"):
    key = (pid, iid, size)
    if key in _bytes:
        return _bytes[key]
    x = next((p for p in _ms_images(pid) if p["id"] == iid), None)
    if not x:
        return None
    href = x["full"] if size == "full" else (x["mini"] or x["full"])
    r = oh.S.get(href, timeout=(10, 60))
    r.raise_for_status()
    val = (r.headers.get("Content-Type", "image/jpeg").split(";")[0], r.content)
    if len(_bytes) > 300:
        _bytes.clear()
    _bytes[key] = val
    return val


def pending(pid):
    with db() as d:
        rows = d.run("SELECT id, filename, status, error, attempts, created FROM card_photo_q WHERE pid=%s AND status<>'sent' ORDER BY created",
                     (pid,), many=True) or []
    return [{"id": i, "filename": f, "status": s, "error": e or "", "attempts": a or 0, "created": c} for i, f, s, e, a, c in rows]


def add_photo(pid, data, mime, who=""):
    """Сохраняет фото у нас и сразу пробует отправить в МойСклад. Ошибка проверки — ValueError."""
    mime = (mime or "").split(";")[0].lower()
    if mime not in PHOTO_TYPES:
        raise ValueError("Фото — JPG, PNG или WEBP")
    if not data or len(data) > PHOTO_MAX:
        raise ValueError("Фото — до 10 МБ")
    with mirror.db() as d:
        if not d.run("SELECT 1 FROM ms_product WHERE id=%s AND deleted=0", (pid,), one=True):
            raise ValueError("Товар не найден")
    op = str(uuid.uuid4())
    with db() as d:
        d.run("INSERT INTO card_photo_q (id, pid, filename, mime, data, status, attempts, error, who, created, sent, next_try)"
              " VALUES (%s, %s, %s, %s, %s, 'queued', 0, '', %s, %s, 0, 0)",
              (op, pid, f"amura-{op[:12]}{PHOTO_TYPES[mime]}", mime, data, who, time.time()))
    return send_photo(op)


def _q(op):
    with db() as d:
        r = d.run("SELECT id, pid, filename, mime, data, status, attempts FROM card_photo_q WHERE id=%s", (op,), one=True)
    return r


def _mark(op, **f):
    sets = ", ".join(f"{k}=%s" for k in f)
    with db() as d:
        d.run(f"UPDATE card_photo_q SET {sets} WHERE id=%s", (*f.values(), op))


def send_photo(op):
    """Одна попытка отправить фото в МойСклад. Второй копии не создаёт: перед повтором ищет файл по имени."""
    with _lock:
        r = _q(op)
        if not r or r[5] == "sent":
            return _state(op)
        _, pid, fname, mime, data, _, att0 = r
        att = (att0 or 0) + 1
        _mark(op, attempts=att)
        try:
            have = _ms_images(pid, fresh=True)
            if not any(x["filename"] == fname for x in have):
                if len(have) >= MS_PHOTOS_MAX:
                    raise RuntimeError(f"МойСклад 400: у товара уже {MS_PHOTOS_MAX} фото — удалите лишнее")
                oh.ms("POST", f"/entity/product/{pid}/images",
                      json=[{"filename": fname, "content": base64.b64encode(bytes(data)).decode()}], timeout=60)
            _imgs.pop(pid, None)
            _mark(op, status="sent", error="", sent=time.time(), data=b"")
            print(f"Карточки: фото {fname} загружено в МойСклад", flush=True)
        except Exception as e:
            msg = str(e)[:300]
            rejected = msg.startswith("МойСклад 4")
            _mark(op, status="error" if rejected else "queued", error=msg,
                  next_try=time.time() + min(3600, RETRY_EVERY * 2 ** min(att, 6)))
            print(f"Карточки: фото {fname} не отправлено (попытка {att}): {msg}", flush=True)
    return _state(op)


def _state(op):
    with db() as d:
        r = d.run("SELECT id, pid, filename, status, error, attempts FROM card_photo_q WHERE id=%s", (op,), one=True)
    return {"id": r[0], "pid": r[1], "filename": r[2], "status": r[3], "error": r[4] or "", "attempts": r[5] or 0} if r else None


def retry_photo(op):
    r = _state(op)
    if r and r["status"] == "error":
        _mark(op, status="queued")
    return send_photo(op)


def delete_photo(pid, iid):
    oh.ms("DELETE", f"/entity/product/{pid}/images/{iid}", timeout=30)
    _imgs.pop(pid, None)
    for k in [k for k in _bytes if k[0] == pid and k[1] == iid]:
        _bytes.pop(k, None)


def retry_pending():
    now = time.time()
    with db() as d:
        ids = [r[0] for r in d.run("SELECT id FROM card_photo_q WHERE status='queued' AND attempts<%s AND next_try<=%s AND created<%s ORDER BY created",
                                   (MAX_AUTO, now, now - 60), many=True) or []]
    for i in ids:
        send_photo(i)
    return len(ids)


# ---------- видео ----------
def video_info(pid):
    with db() as d:
        r = d.run("SELECT size, status, error, name, at FROM product_video WHERE pid=%s", (pid,), one=True)
    if not r:
        return None
    size, st, err, name, at = r
    if st == "processing" and time.time() - (at or 0) > 1800:      # сервер перезапускался посреди сжатия
        st, err = "error", "Сжатие прервалось — загрузите видео ещё раз"
    return {"status": st, "error": err or "", "size": size or 0, "name": name or "", "at": at or 0,
            "url": f"/media/product/{pid}.mp4?v={int(at or 0)}" if st == "ready" else ""}


VIDEO_CACHE = os.path.join(tempfile.gettempdir(), "amura-video")


def video_path(pid):
    """Путь к готовому видео на диске (кэш в /tmp по pid и времени загрузки). Из базы ролик читается один раз —
    дальше отдаётся с диска, без загрузки всего файла в память на каждый просмотр."""
    with db() as d:
        r = d.run("SELECT mime, at FROM product_video WHERE pid=%s AND status='ready'", (pid,), one=True)
    if not r:
        return None
    mime, at = r
    os.makedirs(VIDEO_CACHE, exist_ok=True)
    safe = "".join(ch for ch in pid if ch.isalnum() or ch == "-")[:64]
    path = os.path.join(VIDEO_CACHE, f"{safe}-{int(at or 0)}.mp4")
    if not os.path.exists(path):
        with db() as d:
            row = d.run("SELECT data FROM product_video WHERE pid=%s AND status='ready'", (pid,), one=True)
        if not row:
            return None
        tmp = path + f".{uuid.uuid4().hex[:6]}"
        with open(tmp, "wb") as f:
            f.write(bytes(row[0]))
        os.replace(tmp, path)                      # атомарно: параллельный запрос не увидит недописанный файл
        for old in os.listdir(VIDEO_CACHE):        # старые версии этого ролика — удалить
            if old.startswith(safe + "-") and os.path.join(VIDEO_CACHE, old) != path:
                try:
                    os.remove(os.path.join(VIDEO_CACHE, old))
                except OSError:
                    pass
    return mime, path


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        import shutil
        return shutil.which("ffmpeg")


def _compress(src):
    """Сжимает видео до 720p H.264 + AAC, «быстрый старт» для просмотра в браузере. Возвращает байты или бросает ошибку."""
    import subprocess
    exe = _ffmpeg()
    if not exe:
        raise RuntimeError("на сервере нет ffmpeg")
    out = src + ".mp4"
    try:
        cmd = [exe, "-y", "-loglevel", "error", "-i", src, "-t", str(VIDEO_SECONDS),
               "-vf", "scale='min(720,iw)':-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out]
        try:
            cmd = ["nice", "-n", "10"] + cmd if os.path.exists("/usr/bin/nice") else cmd
        except Exception:
            pass
        r = subprocess.run(cmd, capture_output=True, timeout=900)
        if r.returncode != 0 or not os.path.exists(out):
            raise RuntimeError("не удалось обработать видео: " + (r.stderr.decode(errors="ignore")[-200:] or "ffmpeg"))
        with open(out, "rb") as fh:
            data = fh.read()
        if len(data) > VIDEO_OUT_MAX:
            raise RuntimeError("видео слишком большое даже после сжатия — сократите его")
        return data
    finally:
        for f in (src, out):
            try:
                os.remove(f)
            except OSError:
                pass


def set_video(pid, stream, name="", who="", sync=False):
    """Принимает видео (файловый поток), сохраняет во временный файл и сжимает в фоне. Старое видео заменяется после сжатия."""
    with mirror.db() as d:
        if not d.run("SELECT 1 FROM ms_product WHERE id=%s AND deleted=0", (pid,), one=True):
            raise ValueError("Товар не найден")
    fd, src = tempfile.mkstemp(suffix=os.path.splitext(name or "")[1][:8] or ".bin")
    size = 0
    with os.fdopen(fd, "wb") as f:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > VIDEO_MAX:
                f.close()
                os.remove(src)
                raise ValueError("Видео — до 200 МБ")
            f.write(chunk)
    if not size:
        os.remove(src)
        raise ValueError("Пустой файл")
    now = time.time()
    with db() as d:
        if d.run("SELECT 1 FROM product_video WHERE pid=%s", (pid,), one=True):
            d.run("UPDATE product_video SET status='processing', error='', name=%s, who=%s, at=%s WHERE pid=%s", (name[:120], who, now, pid))
        else:
            d.run("INSERT INTO product_video (pid, mime, data, size, status, error, name, who, at) VALUES (%s, 'video/mp4', %s, 0, 'processing', '', %s, %s, %s)",
                  (pid, b"", name[:120], who, now))

    def work():
        with _vlock:
            try:
                data = _compress(src)
                with db() as d:
                    d.run("UPDATE product_video SET data=%s, size=%s, status='ready', error='', at=%s WHERE pid=%s", (data, len(data), time.time(), pid))
                print(f"Карточки: видео товара {pid[:8]} готово, {len(data) // 1024} КБ", flush=True)
            except Exception as e:
                with db() as d:
                    d.run("UPDATE product_video SET status='error', error=%s WHERE pid=%s", (str(e)[:300], pid))
                print(f"Карточки: видео товара {pid[:8]} не обработано: {e}", flush=True)
    if sync:
        work()
    else:
        threading.Thread(target=work, daemon=True).start()
    return video_info(pid)


def delete_video(pid):
    with db() as d:
        d.run("DELETE FROM product_video WHERE pid=%s", (pid,))
    safe = "".join(ch for ch in pid if ch.isalnum() or ch == "-")[:64]
    if os.path.isdir(VIDEO_CACHE):
        for old in os.listdir(VIDEO_CACHE):
            if old.startswith(safe + "-"):
                try:
                    os.remove(os.path.join(VIDEO_CACHE, old))
                except OSError:
                    pass


def _loop():
    time.sleep(150)
    while True:
        try:
            if enabled():
                retry_pending()
        except Exception as e:
            print("Карточки (очередь фото):", e, flush=True)
        time.sleep(RETRY_EVERY)


if os.environ.get("PORT"):
    threading.Thread(target=_loop, daemon=True).start()
