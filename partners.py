"""Модуль «Заявки на сотрудничество»: «Стать оптовиком» (amura.kz) и «Стать продавцом на AMURA» (amura.kz и opt.amura.kz).
Только сбор заявок — маркетплейс не строим (docs/MARKETPLACE.md). Анти-спам: лимит по адресу и скрытое поле-ловушка.
В системе AMURA — «Клиенты → Заявки»: список, статус (новая / связались / отказ), WhatsApp; владельцу — push о новой заявке."""
import re
import time

from flask import Blueprint, jsonify, request

import admin
import inbox
import modules
import order_hook as oh

bp = Blueprint("partners", __name__)
KINDS = {"opt": "Стать оптовиком", "seller": "Стать продавцом"}
STATUSES = ("new", "contacted", "rejected")
LEGAL = ("ИП", "ТОО", "Пока нет")
_ready = [False]
COLS = "id, kind, name, phone, city, shop, what, legal, comment, status, at, site"


def db():
    d = inbox.db()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        d.run(f"CREATE TABLE IF NOT EXISTS partner_req (id {pk}, kind TEXT, name TEXT, phone TEXT, city TEXT, shop TEXT, what TEXT, legal TEXT,"
              " comment TEXT, status TEXT DEFAULT 'new', at DOUBLE PRECISION, site TEXT)")
        d.c.commit()
        _ready[0] = True
    return d


def on():
    return modules.enabled("partners")


def _s(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


@bp.route("/partners/info")
def info():
    """Для страницы «Сотрудничество»: минимальный заказ на оптовом сайте (если задан)."""
    if not on():
        return jsonify(ok=False), 404
    try:
        mn = oh.opt_min()
    except Exception:
        mn = 0
    return jsonify(ok=True, optMin=mn)


@bp.route("/partners/apply", methods=["POST", "OPTIONS"])
def apply():
    if request.method == "OPTIONS":
        return "", 204
    if not on():
        return jsonify(ok=False, error="Приём заявок сейчас выключен"), 404
    b = request.get_json(silent=True) or {}
    if b.get("website"):                                  # ловушка для ботов: поле скрыто, человек его не заполняет
        return jsonify(ok=True)
    if oh.too_many("partner:" + oh.client_ip(), limit=5, window=3600):
        return jsonify(ok=False, error="Слишком много заявок, попробуйте через час"), 429
    kind = b.get("kind") if b.get("kind") in KINDS else ""
    name, city, phone = _s(b.get("name"), 80), _s(b.get("city"), 80), oh.norm_phone(b.get("phone"))
    if not kind:
        return jsonify(ok=False, error="Выберите, кем хотите стать"), 400
    if not name or not re.fullmatch(r"7\d{10}", phone) or not city:
        return jsonify(ok=False, error="Укажите имя, телефон (+7 7__ ___ __ __) и город"), 400
    if b.get("consent") is not True:
        return jsonify(ok=False, error="Отметьте согласие на обработку персональных данных"), 400
    what = _s(b.get("what"), 300) if kind == "seller" else ""
    if kind == "seller" and not what:
        return jsonify(ok=False, error="Напишите, что вы продаёте"), 400
    legal = b.get("legal") if kind == "seller" and b.get("legal") in LEGAL else ""
    site = "rtl" if b.get("site") == "rtl" else "opt"
    with db() as d:
        rid = d.run("INSERT INTO partner_req (kind, name, phone, city, shop, what, legal, comment, status, at, site) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'new', %s, %s)",
                    (kind, name, phone, city, _s(b.get("shop"), 120), what, legal, _s(b.get("comment"), 1000), time.time(), site), ins=True)
    try:
        import push
        push.notify(f"🤝 Заявка: {KINDS[kind].lower()}", f"{name}, {city}" + (f" · {what[:60]}" if what else ""), "/admin#partners",
                    perm="orders", tag=f"partner{rid}")
    except Exception as e:
        print("Push о заявке:", e, flush=True)
    return jsonify(ok=True)


def _row(r):
    x = dict(zip(COLS.split(", "), r))
    x["kindName"] = KINDS.get(x["kind"], x["kind"])
    return x


@bp.get("/admin/api/partners")
@admin.need("orders")
def partners_list():
    if not on():
        return jsonify(ok=False, error="Модуль «Заявки на сотрудничество» выключен"), 404
    with db() as d:
        rows = d.run(f"SELECT {COLS} FROM partner_req ORDER BY at DESC LIMIT 500", many=True) or []
    out = [_row(r) for r in rows]
    return jsonify(ok=True, items=out, counts={s: sum(1 for x in out if x["status"] == s) for s in STATUSES})


@bp.post("/admin/api/partners/<int:rid>")
@admin.need("orders")
def partners_status(rid):
    if not on():
        return jsonify(ok=False, error="Модуль выключен"), 404
    st = (request.get_json(silent=True) or {}).get("status")
    if st not in STATUSES:
        return jsonify(ok=False, error="Неизвестный статус"), 400
    with db() as d:
        d.run("UPDATE partner_req SET status=%s WHERE id=%s", (st, rid))
    return jsonify(ok=True)
