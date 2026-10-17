"""«Хиты продаж» на розничном сайте (amura.kz), как у WB/Ozon: лента под баннерами и сортировка «Популярные».
Рейтинг — продажи за 30 дней в штуках из нашей копии МойСклад, только заказы с клиентского сайта amura.kz
(«Заказ с розничного сайта»). Касса не считается — через неё пробиваются и оптовые заказы, опт тоже не считается (решение владельца 17.10).
Только товары в наличии и не скрытые (берём из живого каталога). Пересчёт раз в сутки (или «Пересчитать» в системе AMURA).
Владелец закрепляет товары в «Хитах» и исключает их вручную: «Товары → Баннеры → Хиты продаж»."""
import json
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

import admin
import inbox
import mirror
import order_hook as oh

bp = Blueprint("hits", __name__)
MSK = timezone(timedelta(hours=3))
TOP = 20                     # сколько товаров в ленте «Хиты продаж»
POP = 400                    # сколько товаров с рейтингом отдаём сайту для сортировки «Популярные»
TTL = 86400


def _get(key):
    with inbox.db() as d:
        try:
            return json.loads(inbox.get_setting(d, key, "[]") or "[]")
        except ValueError:
            return []


def _put(key, ids):
    with inbox.db() as d:
        inbox.set_setting(d, key, json.dumps(ids[:100]))


def sales(days=30, now=None):
    """{id товара: штук} за days дней — только заказы с клиентского сайта."""
    since = ((now or datetime.now(MSK)) - timedelta(days=days)).astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")
    score = {}
    with mirror.db() as d:
        rows = d.run("SELECT l.product_id, SUM(l.qty) FROM ms_doc_line l JOIN ms_doc x ON x.id = l.doc_id "
                     "WHERE x.deleted = 0 AND x.moment >= %s AND x.type = 'customerorder' AND x.descr = %s "
                     "GROUP BY l.product_id", (since, oh.RETAIL_MARK), many=True) or []
    for pid, qty in rows:
        score[pid] = score.get(pid, 0) + (qty or 0)
    return score


def compute(now=None):
    """Хиты и порядок «Популярные» по живому каталогу (в наличии и не скрытые) с учётом закреплённых и исключённых."""
    items = {i["id"]: i for i in (oh.live() or {}).get("items", [])}
    off, pins = set(_get("hits_off")), [p for p in _get("hits_pin") if p in items]
    try:
        score = sales(now=now)
    except Exception as e:
        print("Хиты: продажи не прочитаны:", e, flush=True)
        score = {}
    ranked = [pid for pid, s in sorted(score.items(), key=lambda kv: -kv[1]) if pid in items and s > 0]
    hits = (pins + [p for p in ranked if p not in pins and p not in off])[:TOP]
    pop = (hits + [p for p in ranked if p not in hits])[:POP]
    return {"hits": hits, "pop": pop, "at": time.time()}


def public():
    """Для /catalog?mode=retail: {"hits": [...], "pop": [...]}; раз в сутки, не дольше секунды на расчёт."""
    try:
        return oh.cached("hits", TTL, compute)
    except Exception as e:
        print("Хиты:", e, flush=True)
        return {"hits": [], "pop": [], "at": 0}


def reset():
    oh._cache.pop("hits", None)
    for k in [k for k in oh._cache if isinstance(k, tuple) and k and k[0] == "catjson"]:
        oh._cache.pop(k, None)


# ---------- система AMURA: Товары → Баннеры → «Хиты продаж» ----------
@bp.get("/admin/api/hits")
@admin.need("products")
def hits_view():
    h = public()
    items = {i["id"]: i for i in (oh.live() or {}).get("items", []) + (oh.live() or {}).get("hiddenItems", [])}
    card = lambda pid: {"id": pid, "name": (items.get(pid) or {}).get("name") or (oh._names.get(pid) or ("Товар снят с сайта",))[0],
                        "img": oh.img_url((items.get(pid) or {}).get("img"))}
    pins, off = _get("hits_pin"), _get("hits_off")
    return jsonify(ok=True, hits=[card(p) | {"pin": p in pins} for p in h["hits"]], off=[card(p) for p in off],
                   at=datetime.fromtimestamp(h["at"], timezone(timedelta(hours=5))).strftime("%d.%m %H:%M") if h["at"] else "")


@bp.post("/admin/api/hits")
@admin.need("products")
def hits_edit():
    """{id, act: pin | unpin | off | on | recalc}: закрепить в «Хитах», открепить, исключить, вернуть, пересчитать сейчас."""
    b = request.get_json(silent=True) or {}
    pid, act = str(b.get("id") or "")[:40], b.get("act")
    pins, off = _get("hits_pin"), _get("hits_off")
    if act == "pin" and pid and pid not in pins:
        pins.insert(0, pid)
        off = [x for x in off if x != pid]
    elif act == "unpin":
        pins = [x for x in pins if x != pid]
    elif act == "off" and pid and pid not in off:
        off.insert(0, pid)
        pins = [x for x in pins if x != pid]
    elif act == "on":
        off = [x for x in off if x != pid]
    elif act != "recalc":
        return jsonify(ok=False, error="Неизвестное действие"), 400
    _put("hits_pin", pins)
    _put("hits_off", off)
    reset()
    return hits_view()
