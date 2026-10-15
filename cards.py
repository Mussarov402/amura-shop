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
    # правка карточки: изменённые поля (JSON) — очередь в МойСклад, как у фото
    d.run("CREATE TABLE IF NOT EXISTS card_edit_q (id TEXT PRIMARY KEY, pid TEXT, changes TEXT, status TEXT, attempts INTEGER DEFAULT 0,"
          " error TEXT, who TEXT, created DOUBLE PRECISION, sent DOUBLE PRECISION, next_try DOUBLE PRECISION DEFAULT 0)")
    # новый товар: данные формы — очередь в МойСклад (externalCode = id, без дублей)
    d.run("CREATE TABLE IF NOT EXISTS card_new_q (id TEXT PRIMARY KEY, data TEXT, status TEXT, pid TEXT, attempts INTEGER DEFAULT 0,"
          " error TEXT, who TEXT, created DOUBLE PRECISION, sent DOUBLE PRECISION, next_try DOUBLE PRECISION DEFAULT 0)")
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
        r = d.run("SELECT id, kind, code, article, name, folder, prices, buy_price, barcodes, archived, buy_raw, buy_cur, uom FROM ms_product"
                  " WHERE id=%s AND deleted=0",
                  (pid,), one=True)
        if not r:
            return None
        names = dict(d.run("SELECT id, name FROM ms_store", many=True) or [])
        stock = d.run("SELECT store_id, stock, reserve FROM ms_stock WHERE product_id=%s", (pid,), many=True) or []
    i, kind, code, art, name, folder, prices, buy, bcs, arch, buy_raw, buy_cur, uom = r
    return {"id": i, "kind": kind or "product", "code": code or "", "article": art or "", "name": name or "", "folder": folder or "",
            "prices": json.loads(prices or "{}"),
            # закупочная цена — в валюте карточки МойСклад (buyCur, пусто — тенге); buyKzt — в тенге по текущему курсу
            "buy": round(buy_raw or 0, 2) if buy_cur else round(buy or 0), "buyCur": buy_cur or "", "buyKzt": round(buy or 0), "barcodes": json.loads(bcs or "[]"), "archived": bool(arch), "uom": uom or "",
            "stock": sorted([{"store": names.get(s, s), "stock": a or 0, "reserve": b or 0} for s, a, b in stock if a or b], key=lambda x: x["store"]),
            "site": f"{oh.SITE_URL.rstrip('/')}/img/{i}.webp" if getattr(oh, "SITE_URL", "") else "",
            "video": video_info(i), "pending": pending(i), "edits": edits_pending(i)}


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


# ---------- правка карточки (название, артикул, цены, закупка, штрихкоды, единица измерения) — в МойСклад ----------
def _bc_type(v):
    return "ean13" if v.isdigit() and len(v) == 13 else "ean8" if v.isdigit() and len(v) == 8 else "code128"


def edit(pid, data, who=""):
    """Проверяет изменения, сохраняет их у нас и сразу отправляет в МойСклад. В МойСклад уходят только изменённые поля;
    повтор безопасен — PUT с теми же значениями ничего не задваивает. Ошибка проверки — ValueError."""
    c = card(pid)
    if not c:
        raise ValueError("Товар не найден")
    ch = {}
    if "name" in data:
        name = str(data["name"] or "").strip()
        if not name or len(name) > 255:
            raise ValueError("Название — от 1 до 255 символов")
        if name != c["name"]:
            ch["name"] = name
    if "article" in data:
        art = str(data["article"] or "").strip()[:255]
        if art != c["article"]:
            ch["article"] = art
    if "buy" in data:
        try:
            buy = round(float(data["buy"]), 2) if c.get("buyCur") else round(float(data["buy"]))
        except (TypeError, ValueError):
            raise ValueError("Закупочная цена — число")
        if buy < 0:
            raise ValueError("Цена не может быть меньше нуля")
        if buy != c["buy"]:
            ch["buy"], ch["buyCur"] = buy, c.get("buyCur", "")
    if "prices" in data:
        pr = {}
        for k, v in (data["prices"] or {}).items():
            if k not in c["prices"]:
                raise ValueError(f"Нет такого вида цены: {k}")
            try:
                v = round(float(v))
            except (TypeError, ValueError):
                raise ValueError("Цены — числа")
            if v < 0:
                raise ValueError("Цена не может быть меньше нуля")
            if v != c["prices"][k]:
                pr[k] = v
        if pr:
            ch["prices"] = pr
    if "barcodes" in data:
        bcs = []
        for b in data["barcodes"] or []:
            b = str(b or "").strip()
            if not b:
                continue
            if len(b) > 64 or not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in b):
                raise ValueError(f"Штрихкод «{b[:20]}» — только цифры и латинские буквы")
            if b not in bcs:
                bcs.append(b)
        if bcs != c["barcodes"]:
            ch["barcodes"] = bcs
    if data.get("uom") and data["uom"] != c.get("uom"):
        if c["kind"] == "variant":
            raise ValueError("Единица измерения меняется в карточке основного товара")
        if data["uom"] not in _uom_names():
            raise ValueError("Выберите единицу измерения из списка")
        ch["uom"] = data["uom"]
    if not ch:
        return {"status": "nothing"}
    op = str(uuid.uuid4())
    with db() as d:
        d.run("INSERT INTO card_edit_q (id, pid, changes, status, attempts, error, who, created, sent, next_try)"
              " VALUES (%s, %s, %s, 'queued', 0, '', %s, %s, 0, 0)", (op, pid, json.dumps(ch, ensure_ascii=False), who, time.time()))
    return send_edit(op)


def _eq(op):
    with db() as d:
        r = d.run("SELECT id, pid, changes, status, attempts, error FROM card_edit_q WHERE id=%s", (op,), one=True)
    return {"id": r[0], "pid": r[1], "changes": json.loads(r[2] or "{}"), "status": r[3], "attempts": r[4] or 0, "error": r[5] or ""} if r else None


def _emark(op, **f):
    sets = ", ".join(f"{k}=%s" for k in f)
    with db() as d:
        d.run(f"UPDATE card_edit_q SET {sets} WHERE id=%s", (*f.values(), op))


def _edit_body(cur, ch):
    """Тело PUT из текущей карточки МойСклад: меняем только изменённое, остальное (другие цены, валюты, типы штрихкодов) — как было."""
    body = {}
    if "name" in ch:
        body["name"] = ch["name"]
    if "article" in ch:
        body["article"] = ch["article"]
    if "buy" in ch:
        bp = dict(cur.get("buyPrice") or {})
        # цена в валюте карточки: если в МойСклад валюту закупки за это время поменяли — не пишем (иначе доллары станут тенге)
        ms_cur = ((bp.get("currency") or {}).get("meta") or {}).get("href", "").rstrip("/").rsplit("/", 1)[-1]
        mirror._load_cur()
        foreign = bool(ms_cur) and ms_cur in mirror._CUR["by_id"] and ms_cur != mirror._CUR["default"]
        if (ch.get("buyCur") and ms_cur != mirror.cur_id(ch["buyCur"])) or (not ch.get("buyCur") and foreign):
            raise ValueError("Валюта закупочной цены в МойСклад изменилась — откройте карточку заново")
        bp["value"] = round(ch["buy"] * 100)
        body["buyPrice"] = bp
    if "prices" in ch:
        sp = []
        for p in cur.get("salePrices") or []:
            p = dict(p)
            name = (p.get("priceType") or {}).get("name")
            if name in ch["prices"]:
                p["value"] = ch["prices"][name] * 100
            sp.append(p)
        body["salePrices"] = sp
    if "barcodes" in ch:
        old = {v: k for b in cur.get("barcodes") or [] for k, v in b.items() if isinstance(v, str)}
        body["barcodes"] = [{old.get(v, _bc_type(v)): v} for v in ch["barcodes"]]
    if "uom" in ch:
        body["uom"] = oh.meta("uom", ch["uom"])
        # упаковка в той же единице МойСклад не допускает («[3007] Упаковка … повторяется») — такую упаковку убираем
        packs = cur.get("packs") or []
        keep = [p for p in packs if mirror._href_id(p.get("uom")) != ch["uom"]]
        if len(keep) != len(packs):
            body["packs"] = keep
    return body


def send_edit(op):
    with _lock:
        o = _eq(op)
        if not o or o["status"] == "sent":
            return o
        att = o["attempts"] + 1
        _emark(op, attempts=att)
        try:
            kind = "variant" if (card(o["pid"]) or {}).get("kind") == "variant" else "product"
            cur = oh.ms("GET", f"/entity/{kind}/{o['pid']}", timeout=30)
            oh.ms("PUT", f"/entity/{kind}/{o['pid']}", json=_edit_body(cur, o["changes"]), timeout=30)
            _emark(op, status="sent", error="", sent=time.time())
            _apply_local(o["pid"], o["changes"])
            print(f"Карточки: правка товара {o['pid'][:8]} записана в МойСклад ({', '.join(o['changes'])})", flush=True)
        except Exception as e:
            msg = str(e)[:300]
            _emark(op, status="error" if msg.startswith("МойСклад 4") else "queued", error=msg,
                   next_try=time.time() + min(3600, RETRY_EVERY * 2 ** min(att, 6)))
            print(f"Карточки: правка товара {o['pid'][:8]} не отправлена (попытка {att}): {msg}", flush=True)
    return _eq(op)


def _apply_local(pid, ch):
    """Сразу показываем новое в системе (зеркало подтянет то же самое из МойСклад при следующем проходе)."""
    with mirror.db() as d:
        r = d.run("SELECT prices FROM ms_product WHERE id=%s", (pid,), one=True)
        if not r:
            return
        if "name" in ch:
            d.run("UPDATE ms_product SET name=%s WHERE id=%s", (ch["name"], pid))
        if "article" in ch:
            d.run("UPDATE ms_product SET article=%s WHERE id=%s", (ch["article"], pid))
        if "buy" in ch:
            rate = mirror.cur_rate(ch.get("buyCur", "")) if ch.get("buyCur") else 1
            d.run("UPDATE ms_product SET buy_raw=%s, buy_price=%s WHERE id=%s", (ch["buy"], round(ch["buy"] * (rate or 0)), pid))
        if "prices" in ch:
            pr = json.loads(r[0] or "{}")
            pr.update(ch["prices"])
            d.run("UPDATE ms_product SET prices=%s WHERE id=%s", (json.dumps(pr, ensure_ascii=False), pid))
        if "barcodes" in ch:
            d.run("UPDATE ms_product SET barcodes=%s WHERE id=%s", (json.dumps(ch["barcodes"], ensure_ascii=False), pid))
        if "uom" in ch:
            d.run("UPDATE ms_product SET uom=%s WHERE id=%s", (ch["uom"], pid))


def edits_pending(pid):
    with db() as d:
        rows = d.run("SELECT id, changes, status, error FROM card_edit_q WHERE pid=%s AND status<>'sent' ORDER BY created", (pid,), many=True) or []
    return [{"id": i, "changes": json.loads(c or "{}"), "status": s, "error": e or ""} for i, c, s, e in rows]


def retry_edit(op):
    o = _eq(op)
    if o and o["status"] == "error":
        _emark(op, status="queued")
    return send_edit(op)


# ---------- новый товар ----------
_meta = {}


def _uom_names():
    c = _meta.get("uom")
    if not c or time.time() - c[0] > 600:
        names, off = {}, 0
        while True:
            rows = oh.ms("GET", "/entity/uom", params={"limit": 1000, "offset": off}, timeout=30).get("rows") or []
            names.update({x.get("id"): x.get("name") or "" for x in rows if x.get("id")})
            if len(rows) < 1000:
                break
            off += 1000
        c = (time.time(), names)
        _meta["uom"] = c
    return c[1]


def uoms(extra=""):
    """Единицы измерения для выбора в карточке: те, что уже стоят у товаров, + extra (в МойСклад их сотни; названия — кэш 10 минут)."""
    names = _uom_names()
    with mirror.db() as d:
        used = {r[0] for r in d.run("SELECT DISTINCT uom FROM ms_product WHERE uom IS NOT NULL AND uom<>''", many=True) or []}
    used |= {extra} if extra else set()
    return sorted([{"id": i, "name": names[i]} for i in used if i in names], key=lambda u: u["name"])


def new_meta():
    """Папки товаров и виды цен из МойСклад для формы нового товара (кэш 10 минут) + следующий свободный код."""
    c = _meta.get("v")
    if not c or time.time() - c[0] > 600:
        folders = [{"id": (x.get("id") or ""), "name": ((x.get("pathName") + "/") if x.get("pathName") else "") + (x.get("name") or "")}
                   for x in oh.ms("GET", "/entity/productfolder", params={"limit": 1000}, timeout=30).get("rows") or [] if not x.get("archived")]
        pts = oh.ms("GET", "/context/companysettings/pricetype", timeout=30)
        pts = pts if isinstance(pts, list) else pts.get("rows", [])
        c = (time.time(), {"folders": sorted(folders, key=lambda f: f["name"]),
                           "priceTypes": [{"id": p.get("id") or "", "name": p.get("name") or ""} for p in pts]})
        _meta["v"] = c
    with mirror.db() as d:
        codes = [r[0] for r in d.run("SELECT code FROM ms_product WHERE kind='product'", many=True) or []]
    nums = [int(x) for x in codes if x and x.isdigit()]
    width = max([len(x) for x in codes if x and x.isdigit()] or [5])
    return {**c[1], "nextCode": str(max(nums or [0]) + 1).zfill(width)}


def create(data, who=""):
    """Новый товар: проверка, сохранение у нас, отправка в МойСклад. Ошибка проверки — ValueError."""
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 255:
        raise ValueError("Название — от 1 до 255 символов")
    code = str(data.get("code") or "").strip()[:64]
    meta = new_meta()
    with mirror.db() as d:
        if code and d.run("SELECT 1 FROM ms_product WHERE code=%s AND deleted=0", (code,), one=True):
            raise ValueError(f"Код {code} уже занят — укажите другой")
        same = any((n or "").strip().lower() == name.lower() for (n,) in d.run("SELECT name FROM ms_product WHERE deleted=0", many=True) or [])
        if same and not data.get("force"):        # сравнение в Python: LOWER() в SQLite не знает кириллицы
            raise ValueError("Товар с таким названием уже есть. Если это другой товар — измените название")
    folder = str(data.get("folder") or "")
    if folder and folder not in {f["id"] for f in meta["folders"]}:
        raise ValueError("Выберите папку из списка")
    pts = {p["name"]: p["id"] for p in meta["priceTypes"]}
    prices = {}
    for k, v in (data.get("prices") or {}).items():
        if k not in pts:
            raise ValueError(f"Нет такого вида цены: {k}")
        try:
            v = round(float(v or 0))
        except (TypeError, ValueError):
            raise ValueError("Цены — числа")
        if v < 0:
            raise ValueError("Цена не может быть меньше нуля")
        prices[k] = v
    try:
        buy = round(float(data.get("buy") or 0))
    except (TypeError, ValueError):
        raise ValueError("Закупочная цена — число")
    if buy < 0:
        raise ValueError("Цена не может быть меньше нуля")
    bcs = []
    for b in data.get("barcodes") or []:
        b = str(b or "").strip()
        if not b:
            continue
        if len(b) > 64 or not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in b):
            raise ValueError(f"Штрихкод «{b[:20]}» — только цифры и латинские буквы")
        if b not in bcs:
            bcs.append(b)
    clean = {"name": name, "code": code, "article": str(data.get("article") or "").strip()[:255], "folder": folder,
             "prices": prices, "buy": buy, "barcodes": bcs, "priceTypes": {k: pts[k] for k in prices}}
    op = str(uuid.uuid4())
    with db() as d:
        d.run("INSERT INTO card_new_q (id, data, status, pid, attempts, error, who, created, sent, next_try)"
              " VALUES (%s, %s, 'queued', '', 0, '', %s, %s, 0, 0)", (op, json.dumps(clean, ensure_ascii=False), who, time.time()))
    return send_new(op)


def _nq(op):
    with db() as d:
        r = d.run("SELECT id, data, status, pid, attempts, error FROM card_new_q WHERE id=%s", (op,), one=True)
    return {"id": r[0], "data": json.loads(r[1] or "{}"), "status": r[2], "pid": r[3] or "", "attempts": r[4] or 0, "error": r[5] or ""} if r else None


def _nmark(op, **f):
    sets = ", ".join(f"{k}=%s" for k in f)
    with db() as d:
        d.run(f"UPDATE card_new_q SET {sets} WHERE id=%s", (*f.values(), op))


def _new_body(op, x):
    body = {"name": x["name"], "externalCode": op}
    if x.get("code"):
        body["code"] = x["code"]
    if x.get("article"):
        body["article"] = x["article"]
    if x.get("folder"):
        body["productFolder"] = oh.meta("productfolder", x["folder"])
    if x.get("prices"):
        body["salePrices"] = [{"value": v * 100, "priceType": {"meta": {"href": f"{oh.API}/context/companysettings/pricetype/{x['priceTypes'][k]}",
                                                                        "type": "pricetype", "mediaType": "application/json"}}} for k, v in x["prices"].items()]
    if x.get("buy"):
        body["buyPrice"] = {"value": x["buy"] * 100}
    if x.get("barcodes"):
        body["barcodes"] = [{_bc_type(b): b} for b in x["barcodes"]]
    return body


def send_new(op):
    """Одна попытка создать товар в МойСклад. Перед повтором ищет по externalCode — второй карточки не будет."""
    with _lock:
        o = _nq(op)
        if not o or o["status"] == "sent":
            return o
        att = o["attempts"] + 1
        _nmark(op, attempts=att)
        try:
            row = None
            if o["attempts"]:
                found = oh.ms("GET", "/entity/product", params={"filter": f"externalCode={op}", "limit": 1}, timeout=20).get("rows") or []
                row = found[0] if found else None
            if row is None:
                row = oh.ms("POST", "/entity/product", json=_new_body(op, o["data"]), timeout=30)
            pid = row.get("id") or ""
            _nmark(op, status="sent", pid=pid, error="", sent=time.time())
            _new_local(pid, row, o["data"])
            print(f"Карточки: товар «{o['data']['name'][:40]}» создан в МойСклад ({pid[:8]})", flush=True)
        except Exception as e:
            msg = str(e)[:300]
            _nmark(op, status="error" if msg.startswith("МойСклад 4") else "queued", error=msg,
                   next_try=time.time() + min(3600, RETRY_EVERY * 2 ** min(att, 6)))
            print(f"Карточки: товар «{o['data']['name'][:40]}» не создан (попытка {att}): {msg}", flush=True)
    return _nq(op)


def _new_local(pid, row, x):
    """Новый товар сразу в нашей копии — карточка открывается без ожидания зеркала (оно потом перезапишет то же)."""
    if not pid:
        return
    with mirror.db() as d:
        if d.run("SELECT 1 FROM ms_product WHERE id=%s", (pid,), one=True):
            return
        d.run("INSERT INTO ms_product (id, kind, parent_id, code, article, name, folder, archived, prices, buy_price, barcodes, updated, deleted, seen)"
              " VALUES (%s, 'product', NULL, %s, %s, %s, %s, 0, %s, %s, %s, %s, 0, %s)",
              (pid, row.get("code") or x.get("code") or "", x.get("article") or "", x["name"], row.get("pathName") or "",
               json.dumps(x.get("prices") or {}, ensure_ascii=False), x.get("buy") or 0, json.dumps(x.get("barcodes") or [], ensure_ascii=False),
               (row.get("updated") or "")[:23], time.time()))


def retry_new(op):
    o = _nq(op)
    if o and o["status"] == "error":
        _nmark(op, status="queued")
    return send_new(op)


def new_pending():
    with db() as d:
        rows = d.run("SELECT id, data, status, error FROM card_new_q WHERE status<>'sent' ORDER BY created", many=True) or []
    return [{"id": i, "name": json.loads(dt or "{}").get("name", ""), "status": s, "error": e or ""} for i, dt, s, e in rows]


def _loop():
    time.sleep(150)
    while True:
        try:
            if enabled():
                retry_pending()
                now = time.time()
                with db() as d:
                    ids = [r[0] for r in d.run("SELECT id FROM card_edit_q WHERE status='queued' AND attempts<%s AND next_try<=%s AND created<%s",
                                               (MAX_AUTO, now, now - 60), many=True) or []]
                for i in ids:
                    send_edit(i)
                with db() as d:
                    ids = [r[0] for r in d.run("SELECT id FROM card_new_q WHERE status='queued' AND attempts<%s AND next_try<=%s AND created<%s",
                                               (MAX_AUTO, now, now - 60), many=True) or []]
                for i in ids:
                    send_new(i)
        except Exception as e:
            print("Карточки (очередь фото):", e, flush=True)
        time.sleep(RETRY_EVERY)


if os.environ.get("PORT"):
    threading.Thread(target=_loop, daemon=True).start()
