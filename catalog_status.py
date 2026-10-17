"""«Оприходовано, ещё не на сайте» (Товары → «Товары»): товары с остатком в МойСклад (обычно после приёмки или оприходования),
которых нет в текущей выгрузке catalog.json или там остаток 0, и кнопка «Выгрузить на сайт сейчас» (GitHub Actions, catalog.yml).
Правила витрины не меняем. Если товар и после выгрузки не выйдет из-за фильтров export_catalog.py — короткая пометка у строки
(«не выйдет: нет цены» / «в архиве» / «служебный код»; скрытый в системе AMURA — «скрыт в витрине»).
Данные — копия МойСклад (ms_product), свежие остатки и catalog.json с сайта. Только чтение."""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, jsonify

import admin
import mirror
import order_hook as oh

bp = Blueprint("catalog_status", __name__)
REPO = os.environ.get("GH_REPO", "Mussarov402/amura-shop")
WORKFLOW = "catalog.yml"
GH = "https://api.github.com"
ALMATY = timezone(timedelta(hours=5))
COOLDOWN = 120
_last_dispatch = [0.0]


def _skip_codes():
    return {oh.LOADER_CODE, "00308"}                          # как SKIP_CODES в export_catalog.py («Услуга грузчика»)


def _exported():
    """{id: остаток} из текущего catalog.json на сайте (кэш 2 минуты)."""
    def load():
        r = requests.get(f"{oh.SITE_URL}/catalog.json", timeout=20, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        return {i["id"]: int(i.get("qty") or 0) for i in r.json().get("items", [])}
    return oh.cached("exported_qty", 120, load)


def pending(products=None, exported=None, stock=None, hidden=None, live=None):
    """Товары с остатком, которых нет в catalog.json (или там 0). У строки note — почему не выйдет и после выгрузки."""
    if products is None:
        with mirror.db() as d:
            products = [dict(zip(("id", "code", "name", "archived", "prices"), r)) for r in
                        d.run("SELECT id, code, name, archived, prices FROM ms_product WHERE kind='product' AND deleted=0", many=True) or []]
    exported = exported if exported is not None else _exported()
    stock = stock if stock is not None else oh.cached("nos_stock", 60, oh._stock)
    hidden = hidden if hidden is not None else admin.hidden_ids()
    live = live if live is not None else oh.live()
    imgs = {i["id"]: i.get("img") for i in live.get("items", []) + live.get("hiddenItems", []) + live.get("outItems", [])}
    skip, rows = _skip_codes(), []
    for p in products:
        pid = p["id"]
        qty = int(stock.get(pid, 0) or 0)
        if qty <= 0 or exported.get(pid, 0) > 0:
            continue
        pr = p["prices"] if isinstance(p["prices"], dict) else json.loads(p["prices"] or "{}")
        note = ("служебный код" if (p["code"] or "") in skip else "в архиве" if p.get("archived")
                else "нет цены" if int(pr.get(oh.PRICE_RTL) or 0) <= 0 and int(pr.get(oh.PRICE_OPT) or 0) <= 0
                else "скрыт в витрине" if pid in hidden else "")
        if note == "служебный код":
            continue                                         # «Услуга грузчика» — не товар витрины
        img = imgs.get(pid)
        rows.append({"id": pid, "name": p["name"], "code": p["code"] or "", "qty": qty, "note": note,
                     "img": f"{oh.SITE_URL}/{img}" if img else ""})
    rows.sort(key=lambda x: (bool(x["note"]), x["name"]))
    return rows


# ---------- выгрузка на сайт: GitHub Actions (catalog.yml) ----------
def gh_token():
    return os.environ.get("GH_ACTIONS_TOKEN", "").strip()


def _gh(method, path, **kw):
    return requests.request(method, f"{GH}/repos/{REPO}{path}", headers={"Authorization": f"Bearer {gh_token()}", "Accept": "application/vnd.github+json",
                                                                         "X-GitHub-Api-Version": "2022-11-28"}, timeout=15, **kw)


def last_run():
    """Последний запуск выгрузки: идёт / готово / ошибка и время (Алматы)."""
    if not gh_token():
        return None
    try:
        r = _gh("GET", f"/actions/workflows/{WORKFLOW}/runs", params={"per_page": 1})
        runs = r.json().get("workflow_runs") or [] if r.ok else []
    except Exception:
        return None
    if not runs:
        return None
    x = runs[0]
    st = "running" if x.get("status") != "completed" else ("ok" if x.get("conclusion") == "success" else "error")
    t = x.get("updated_at") or x.get("created_at") or ""
    try:
        at = datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(ALMATY).strftime("%d.%m %H:%M")
    except ValueError:
        at = ""
    return {"status": st, "at": at, "manual": x.get("event") == "workflow_dispatch"}


@bp.get("/admin/api/not-on-site")
@admin.guard
def not_on_site():
    try:
        rows = pending()
    except Exception as e:
        return jsonify(ok=False, error="Каталог или копия МойСклад сейчас недоступны: " + str(e)[:150]), 503
    return jsonify(ok=True, rows=rows[:500], total=len(rows), key=bool(gh_token()), run=last_run(),
                   wait=max(0, int(COOLDOWN - (time.time() - _last_dispatch[0]))))


@bp.get("/admin/api/catalog/publish")
@admin.guard
def publish_status():
    return jsonify(ok=True, key=bool(gh_token()), run=last_run(), wait=max(0, int(COOLDOWN - (time.time() - _last_dispatch[0]))))


@bp.post("/admin/api/catalog/publish")
@admin.guard
def publish():
    """«Выгрузить на сайт сейчас» — запуск catalog.yml вручную. Не чаще раза в 2 минуты."""
    if not gh_token():
        return jsonify(ok=False, error="Нужен ключ GitHub (GH_ACTIONS_TOKEN) — см. Настройки → Модули"), 400
    left = COOLDOWN - (time.time() - _last_dispatch[0])
    if left > 0:
        return jsonify(ok=False, error=f"Выгрузка уже запущена — следующую можно через {int(left) + 1} с"), 429
    try:
        r = _gh("POST", f"/actions/workflows/{WORKFLOW}/dispatches", json={"ref": "main"})
    except Exception as e:
        return jsonify(ok=False, error="GitHub не ответил: " + str(e)[:150]), 502
    if r.status_code != 204:
        return jsonify(ok=False, error=f"GitHub отказал ({r.status_code}): " + (r.text or "")[:200]), 502
    _last_dispatch[0] = time.time()
    return jsonify(ok=True, message="Выгрузка запущена, обычно 2–5 минут")
