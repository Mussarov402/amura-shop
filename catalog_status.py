"""«Нет на сайте»: какие товары МойСклад не видит покупатель на amura.kz / opt.amura.kz и почему, плюс кнопка «Выгрузить на сайт сейчас».

Правда о витрине (что реально отсекает товар):
- выгрузка catalog.json (export_catalog.py): только товары (не модификации), не в архиве, с остатком > 0, с розничной или оптовой ценой,
  не служебный код (SKIP_CODES);
- живой каталог сервера (order_hook.build_live): остаток пересчитывается сейчас, скрытые в системе AMURA («Сайт: скрыт») убираются,
  изменённые в МойСклад после выгрузки подхватываются сразу, но без фото (фото — только после выгрузки);
- сайт показывает товар, если остаток > 0 и есть цена своего сайта: розница — розничная, опт — оптовая.
Нет фото — товар всё равно виден (с заглушкой), поэтому это отдельный список «На сайте без фото».
Данные — копия МойСклад (ms_product) + живой каталог + свежие остатки. Только чтение; выгрузку запускает GitHub Actions."""
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
REASONS = {"new": "Новый — ещё не выгружен", "stock": "Нет остатка", "price": "Нет цены", "rtl": "Нет розничной цены — не виден на amura.kz",
           "opt": "Нет оптовой цены — не виден на opt.amura.kz", "hidden": "Скрыт в витрине", "archived": "В архиве", "skip": "Служебный код"}
COOLDOWN = 120
_last_dispatch = [0.0]


def _skip_codes():
    return {oh.LOADER_CODE, "00308"}                          # как SKIP_CODES в export_catalog.py («Услуга грузчика»)


def report(products=None, live=None, stock=None, hidden=None):
    """{counts, rows, noPhoto}. rows — товары, которых нет хотя бы на одном сайте, с причиной."""
    if products is None:
        with mirror.db() as d:
            products = [dict(zip(("id", "code", "article", "name", "archived", "prices", "updated"), r)) for r in
                        d.run("SELECT id, code, article, name, archived, prices, updated FROM ms_product WHERE kind='product' AND deleted=0",
                              many=True) or []]
    live = live if live is not None else oh.live()
    stock = stock if stock is not None else oh.cached("nos_stock", 60, oh._stock)
    hidden = hidden if hidden is not None else admin.hidden_ids()
    skip = _skip_codes()
    site = {i["id"]: i for i in live.get("items", [])}
    rows, counts, nophoto = [], {k: 0 for k in REASONS}, []
    for p in products:
        pid, pr = p["id"], p["prices"] if isinstance(p["prices"], dict) else json.loads(p["prices"] or "{}")
        rtl, opt = int(pr.get(oh.PRICE_RTL) or 0), int(pr.get(oh.PRICE_OPT) or 0)
        qty = int(stock.get(pid, 0) or 0)
        it = site.get(pid)
        if it:                                               # на сайте — но, может быть, не на обоих
            if not it.get("img"):
                nophoto.append({"id": pid, "name": p["name"], "code": p["code"] or "", "qty": qty})
            for k, ok in (("rtl", (it.get("rtl") or 0) > 0), ("opt", (it.get("opt") or 0) > 0)):   # цены — как видит витрина сейчас
                if not ok:
                    counts[k] += 1
                    rows.append({"id": pid, "name": p["name"], "code": p["code"] or "", "qty": qty, "reason": k})
            continue
        if (p["code"] or "") in skip:
            r = "skip"
        elif p.get("archived"):
            r = "archived"
        elif pid in hidden:
            r = "hidden"
        elif qty <= 0:
            r = "stock"
        elif rtl <= 0 and opt <= 0:
            r = "price"
        else:
            r = "new"                                          # всё есть, но витрина его ещё не видит — ждёт выгрузки
        counts[r] += 1
        rows.append({"id": pid, "name": p["name"], "code": p["code"] or "", "qty": qty, "reason": r, "updated": (p.get("updated") or "")[:16]})
    order = list(REASONS)
    rows.sort(key=lambda x: (order.index(x["reason"]), -x["qty"], x["name"]))
    actionable = sum(counts[k] for k in ("new", "price", "rtl", "opt", "hidden"))   # с остатком, но не видны — то, что можно поправить
    return {"counts": counts, "reasons": REASONS, "actionable": actionable, "rows": rows, "noPhoto": nophoto[:500]}


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
        r = report()
    except Exception as e:
        return jsonify(ok=False, error="Копия МойСклад или каталог сейчас недоступны: " + str(e)[:150]), 503
    rows = r.pop("rows")
    return jsonify(ok=True, **r, rows=rows[:1500], total=len(rows), siteUpdated=oh._cache.get("site_updated", ""), mirror=mirror.enabled(),
                   key=bool(gh_token()), run=last_run())


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
