"""Подсказка в Товары → «Товары»: товары с остатком в МойСклад, которые на сайт не выйдут, и почему
(«нет цены» / «в архиве» / «скрыт в витрине»). Оприходованные товары сайт показывает сам за 2–4 минуты
(order_hook._missing), так что обычно список пуст. Данные — копия МойСклад (ms_product) и живой каталог. Только чтение."""
import json

from flask import Blueprint, jsonify

import admin
import mirror
import order_hook as oh

bp = Blueprint("catalog_status", __name__)


def _skip_codes():
    return {oh.LOADER_CODE, "00308"}                          # как SKIP_CODES в export_catalog.py («Услуга грузчика»)


def pending(products=None, stock=None, hidden=None, live=None):
    """Товары с остатком, которых нет на сайте по правилам витрины. У каждой строки note — почему."""
    if products is None:
        with mirror.db() as d:
            products = [dict(zip(("id", "code", "name", "archived", "prices"), r)) for r in
                        d.run("SELECT id, code, name, archived, prices FROM ms_product WHERE kind='product' AND deleted=0", many=True) or []]
    live = live if live is not None else oh.live()
    stock = stock if stock is not None else {i["id"]: i.get("qty", 0) for i in live.get("items", []) + live.get("hiddenItems", [])} | \
        oh.cached("nos_stock", 60, oh._stock)
    hidden = hidden if hidden is not None else admin.hidden_ids()
    shown = {i["id"] for i in live.get("items", [])}
    imgs = {i["id"]: i.get("img") for i in live.get("items", []) + live.get("hiddenItems", []) + live.get("outItems", [])}
    skip, rows = _skip_codes(), []
    for p in products:
        pid = p["id"]
        qty = int(stock.get(pid, 0) or 0)
        if qty <= 0 or pid in shown or (p["code"] or "") in skip:
            continue
        pr = p["prices"] if isinstance(p["prices"], dict) else json.loads(p["prices"] or "{}")
        note = ("в архиве" if p.get("archived")
                else "нет цены" if int(pr.get(oh.PRICE_RTL) or 0) <= 0 and int(pr.get(oh.PRICE_OPT) or 0) <= 0
                else "скрыт в витрине" if pid in hidden else "")
        if not note:
            continue                                         # выйдет сам при следующем обновлении сайта (до 4 минут)
        rows.append({"id": pid, "name": p["name"], "code": p["code"] or "", "qty": qty, "note": note, "img": oh.img_url(imgs.get(pid))})
    rows.sort(key=lambda x: (x["note"], x["name"]))
    return rows


@bp.get("/admin/api/not-on-site")
@admin.guard
def not_on_site():
    try:
        rows = pending()
    except Exception as e:
        return jsonify(ok=False, error="Каталог или копия МойСклад сейчас недоступны: " + str(e)[:150]), 503
    return jsonify(ok=True, rows=rows[:500], total=len(rows))
