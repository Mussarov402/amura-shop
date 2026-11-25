"""Синяя галочка «оригинал» на розничном сайте (владелец 25.11.2026): кому показывать — решает владелец в системе AMURA.

Правило: свой переключатель товара → правило бренда → по умолчанию ВКЛ (как было до 25.11: галочка у всех товаров с брендом).
Правило бренда действует и на новые товары бренда. У бренда и товара — необязательная пометка «документ» (ссылка или
«есть документы поставки»). Флаг на товаре — поле `orig` в живом каталоге (/live), сайт показывает галочку, если orig не false.
Хранение — настройка системы `orig_badge` (как правила «не выставлять» у Ozon и WB). Только владелец."""
import json

from flask import Blueprint, jsonify, request

import inbox
import order_hook as oh

bp = Blueprint("badge", __name__)
KEY = "orig_badge"
DEFAULT = True


def conf():
    with inbox.db() as d:
        raw = inbox.get_setting(d, KEY, "")
    try:
        c = json.loads(raw) if raw else {}
    except ValueError:
        c = {}
    return {"brands": c.get("brands") or {}, "items": c.get("items") or {}}


def _save(c):
    with inbox.db() as d:
        inbox.set_setting(d, KEY, json.dumps(c, ensure_ascii=False))
    oh._cache.pop("live", None)                          # каталог пересоберётся с новыми флагами


def flag(item, c=None):
    """→ (показывать галочку, откуда: item | brand | default)."""
    c = c or conf()
    x = c["items"].get(str(item.get("id")))
    if x and "on" in x:
        return bool(x["on"]), "item"
    b = c["brands"].get(str(item.get("brand") or "").strip().lower())
    if b and "on" in b:
        return bool(b["on"]), "brand"
    return DEFAULT, "default"


def apply(data):
    """Живой каталог: каждому товару поле orig (True/False)."""
    c = conf()
    for key in ("items", "hiddenItems", "outItems"):
        for i in data.get(key) or []:
            i["orig"] = flag(i, c)[0]
    return data


def set_brand(brand, on=None, doc=None):
    c, b = conf(), " ".join(str(brand or "").split())
    if not b:
        raise ValueError("Укажите бренд")
    r = c["brands"].setdefault(b.lower(), {"name": b})
    if on is not None:
        r["on"] = bool(on)
    if doc is not None:
        r["doc"] = str(doc).strip()[:300]
    _save(c)
    return c


def set_items(ids, on=None, doc=None, reset=False):
    c = conf()
    for i in [str(x).strip() for x in ids if str(x or "").strip()][:5000]:
        if reset:
            r = c["items"].get(i) or {}
            r.pop("on", None)
            if r.get("doc"):
                c["items"][i] = r
            else:
                c["items"].pop(i, None)
            continue
        r = c["items"].setdefault(i, {})
        if on is not None:
            r["on"] = bool(on)
        if doc is not None:
            r["doc"] = str(doc).strip()[:300]
    _save(c)
    return c


def rows():
    """Товары витрины (в наличии, скрытые и без остатка) с флагом и источником."""
    data, c = oh.live(max_age=600), conf()
    seen, out = set(), []
    for key in ("items", "hiddenItems", "outItems"):
        for i in data.get(key) or []:
            if i["id"] in seen:
                continue
            seen.add(i["id"])
            on, src = flag(i, c)
            x = c["items"].get(i["id"]) or {}
            out.append({"id": i["id"], "code": i.get("code", ""), "name": i.get("name", ""), "brand": i.get("brand", ""),
                        "qty": i.get("qty", 0), "on": on, "src": src, "doc": x.get("doc", "")})
    out.sort(key=lambda r: (r["brand"].lower(), r["name"].lower()))
    return out, c


# ---------- API (только владелец) ----------
def _owner():
    import admin
    if not admin.who():
        return jsonify(ok=False, error="Войдите заново"), 401
    if not admin.is_admin():
        return jsonify(ok=False, error="Галочка «оригинал» — только владелец"), 403
    return None


@bp.get("/admin/api/badge")
def route_get():
    err = _owner()
    if err:
        return err
    try:
        r, c = rows()
    except Exception as e:
        return jsonify(ok=False, error="Каталог не получен: " + str(e)[:200]), 503
    return jsonify(ok=True, items=r, brands=[{"key": k, **v} for k, v in sorted(c["brands"].items())], default=DEFAULT,
                   on=sum(1 for x in r if x["on"]), off=sum(1 for x in r if not x["on"]))


@bp.put("/admin/api/badge")
def route_put():
    err = _owner()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    on = b.get("on") if isinstance(b.get("on"), bool) else None
    doc = b.get("doc") if "doc" in b else None
    try:
        if b.get("brand"):
            set_brand(b["brand"], on, doc)
        elif isinstance(b.get("ids"), list) and b["ids"]:
            set_items(b["ids"], on, doc, reset=bool(b.get("reset")))
        else:
            return jsonify(ok=False, error="Укажите бренд или товары"), 400
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True)
