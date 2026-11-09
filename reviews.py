"""Отзывы о товарах (розничный сайт amura.kz) — хранятся в нашей базе, в МойСклад не попадают.

Оставить отзыв может только тот, кто покупал этот товар у нас (владелец 09.11.2026): товар есть в его неотменённых заказах
МойСклад (сайт, опт, розница). Не покупал — 403; МойСклад не ответил — 503, без проверки не принимаем.
На сайте — только подтверждённые (verified=1); старые без покупки остаются в системе AMURA с пометкой «без покупки». Модуль включается в панели (Товары → Отзывы);
по умолчанию новые отзывы ждут проверки и появляются на сайте после «Опубликовать». Владельцу — сообщение в Telegram.
"""
import re
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

import inbox
import order_hook as oh

bp = Blueprint("reviews", __name__)
bp.after_request(oh.cors)
_ready = [False]


def db():
    d = inbox.db()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        d.run(f"CREATE TABLE IF NOT EXISTS review (id {pk}, pid TEXT, pname TEXT, cid TEXT, name TEXT, city TEXT, rating INTEGER, "
              "text TEXT, verified INTEGER DEFAULT 0, status TEXT DEFAULT 'new', answer TEXT DEFAULT '', at DOUBLE PRECISION)")
        d.run("CREATE INDEX IF NOT EXISTS review_pid ON review (pid)")
        d.c.commit()
        _ready[0] = True
    return d


def conf():
    with db() as d:
        return {"on": inbox.get_setting(d, "reviews_on", "1") == "1", "premod": inbox.get_setting(d, "reviews_premod", "1") == "1"}


def _row(r, admin=False):
    out = {"name": r[1] or "Покупатель", "city": r[2] or "", "rating": r[3], "text": r[4] or "", "verified": bool(r[5]),
           "answer": r[6] or "", "date": datetime.fromtimestamp(r[7], oh.ALMATY).strftime("%d.%m.%Y")}
    if admin:
        out.update(id=r[0], status=r[8], pid=r[9], pname=r[10] or "")
    return out


COLS = "id, name, city, rating, text, verified, answer, at, status, pid, pname"


def summary():
    def load():
        with db() as d:
            rows = d.run("SELECT pid, AVG(rating), COUNT(*) FROM review WHERE status='ok' AND verified=1 GROUP BY pid", many=True)
        return {r[0]: [round(float(r[1]), 1), int(r[2])] for r in rows}
    return oh.cached("rv_sum", 120, load)


@bp.get("/reviews/summary")
def summary_route():
    if not conf()["on"]:
        return jsonify(ok=True, on=False, items={})
    return jsonify(ok=True, on=True, items=summary())


@bp.get("/reviews")
def list_route():
    pid = str(request.args.get("id", ""))[:64]
    if not conf()["on"]:
        return jsonify(ok=True, on=False, items=[], avg=0, count=0)
    with db() as d:
        rows = d.run(f"SELECT {COLS} FROM review WHERE pid=%s AND status='ok' AND verified=1 ORDER BY at DESC LIMIT 50", (pid,), many=True)
        mine = None
        cid = oh.session_cid()
        if cid:
            m = d.run(f"SELECT {COLS} FROM review WHERE pid=%s AND cid=%s", (pid, cid), one=True)
            mine = _row(m, admin=True) if m else None
    s = summary().get(pid, [0, 0])
    if mine:
        mine = {k: mine[k] for k in ("rating", "text", "status")}
    can = None
    if cid:
        try:
            can = bought(cid, pid)
        except Exception as e:
            print("Отзывы: проверка покупки не удалась:", str(e)[:200], flush=True)
    return jsonify(ok=True, on=True, avg=s[0], count=s[1], items=[_row(r) for r in rows], mine=mine, can_review=can)


CANCEL = ("отмен", "отказ", "аннул")                       # «Отменён», «Отмена», «Отказ клиента» — покупкой не считаются
MAX_ORDERS = 1000


def purchased(cid):
    """Товары во всех неотменённых заказах покупателя в МойСклад (сайт, опт, розница) → множество id. Кэш 5 минут на покупателя.
    Ошибка МойСклад — исключение (отзыв без проверки не принимаем)."""
    def load():
        got, off = set(), 0
        while off < MAX_ORDERS:
            rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"agent={oh.API}/entity/counterparty/{cid}", "limit": 100,
                                                                 "offset": off, "expand": "positions.assortment,state"}, timeout=20)["rows"]
            for o in rows:
                st = str((o.get("state") or {}).get("name") or "").lower()
                if any(w in st for w in CANCEL):
                    continue
                for p in (o.get("positions") or {}).get("rows") or []:
                    a = p.get("assortment") or {}
                    if a.get("id"):
                        got.add(a["id"])
                    prod = (a.get("product") or {}).get("meta", {}).get("href", "")   # модификация — считаем покупку товара
                    if prod:
                        got.add(prod.rstrip("/").rsplit("/", 1)[-1])
            if len(rows) < 100:
                break
            off += 100
        return got
    return oh.cached("rv_bought:" + cid, 300, load)


def bought(cid, pid):
    """Покупал ли покупатель этот товар (неотменённый заказ). Ошибка МойСклад — исключение."""
    return pid in purchased(cid)


@bp.route("/reviews", methods=["POST", "OPTIONS"])
def add_route():
    if request.method == "OPTIONS":
        return "", 204
    c = conf()
    if not c["on"]:
        return jsonify(ok=False, error="Отзывы сейчас выключены"), 403
    cid = oh.session_cid()
    if not cid:
        return jsonify(ok=False, error="Войдите, чтобы оставить отзыв", login=True), 401
    d0 = request.get_json(silent=True) or {}
    pid = re.sub(r"[^\w-]", "", str(d0.get("id", "")))[:64]
    try:
        rating = int(d0.get("rating", 0))
    except (TypeError, ValueError):
        rating = 0
    text = re.sub(r"\s+\n", "\n", str(d0.get("text", ""))).strip()[:2000]
    if not pid or not 1 <= rating <= 5:
        return jsonify(ok=False, error="Поставьте оценку от 1 до 5 звёзд"), 400
    if rating <= 3 and len(text) < 10:
        return jsonify(ok=False, error="Расскажите, что не понравилось — хотя бы пару слов"), 400
    if oh.too_many("rv:" + cid, limit=10, window=3600):
        return jsonify(ok=False, error="Слишком много отзывов подряд, попробуйте позже"), 429
    try:
        cat = oh.catalog()
    except Exception:
        cat = {}
    pname = (cat.get(pid) or {}).get("name", "")
    try:
        prof = oh.profile(oh.ms("GET", f"/entity/counterparty/{cid}"))
    except Exception:
        prof = {"name": "", "city": ""}
    name = (prof.get("name") or "").split(" ")[0][:40]
    try:
        ok = bought(cid, pid)
    except Exception as e:
        print("Отзывы: проверка покупки не удалась:", str(e)[:200], flush=True)
        return jsonify(ok=False, error="Не получилось проверить покупку, попробуйте позже"), 503
    if not ok:
        return jsonify(ok=False, error="Отзыв могут оставить только покупатели этого товара", buyers=True), 403
    verified = 1
    status = "new" if c["premod"] else "ok"
    with db() as d:
        old = d.run("SELECT id FROM review WHERE pid=%s AND cid=%s", (pid, cid), one=True)
        if old:
            d.run("UPDATE review SET rating=%s, text=%s, name=%s, city=%s, verified=%s, status=%s, pname=%s, at=%s WHERE id=%s",
                  (rating, text, name, prof.get("city", "")[:40], verified, status, pname, time.time(), old[0]))
        else:
            d.run("INSERT INTO review (pid, pname, cid, name, city, rating, text, verified, status, at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (pid, pname, cid, name, prof.get("city", "")[:40], rating, text, verified, status, time.time()))
    oh._cache.pop("rv_sum", None)
    if oh.notif_on("review"):
        stars = "★" * rating + "☆" * (5 - rating)
        msg = f"⭐ Новый отзыв {stars}\n{pname or pid}\n{name or 'Покупатель'}{' · купил на сайте' if verified else ''}\n{text[:500]}"
        if status == "new":
            msg += "\n\nОпубликовать: панель → Товары → Отзывы"
        oh.notify_bg(lambda: oh.tg("sendMessage", chat_id=oh.OWNER, text=msg))
    return jsonify(ok=True, status=status)
