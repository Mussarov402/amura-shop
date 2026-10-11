"""Склад: инвентаризация из системы AMURA (этап 3 дорожной карты, docs/MIGRATION.md).

Как устроено:
- пересчёт ведётся черновиком у нас (таблица wh_inv): склад, товары и фактическое количество; черновик сохраняется
  после каждого изменения — можно считать с телефона частями, закрыть и продолжить;
- считаются только добавленные товары: то, что не пересчитали, не трогается (частичная инвентаризация);
- «Провести»: учётный остаток берётся из МойСклад в этот момент (отчёт текущих остатков, не наше зеркало —
  оно отстаёт до 10 минут), снимок сохраняется, расхождения проводятся обычными складскими операциями:
  излишки — оприходованием по закупочной цене, недостача — списанием (whops: очередь, повтор, без дублей);
- id этих операций выводятся из id инвентаризации, поэтому повторное «Провести» после сбоя не создаёт вторых документов.
МойСклад остаётся главным: остатки меняют его документы. Работает при включённых «Складе» и складских операциях."""
import json
import threading
import time
import uuid

import inbox
import mirror
import order_hook as oh
import whops

_ready = [False]
_lock = threading.Lock()


def _schema(d):
    if _ready[0]:
        return
    d.run("CREATE TABLE IF NOT EXISTS wh_inv (id TEXT PRIMARY KEY, store_id TEXT, lines TEXT, status TEXT, descr TEXT, who TEXT,"
          " created DOUBLE PRECISION, updated DOUBLE PRECISION, done DOUBLE PRECISION, enter_op TEXT, loss_op TEXT, error TEXT)")
    d.c.commit()
    _ready[0] = True


def db():
    d = inbox.db()
    _schema(d)
    return d


_COLS = "id, store_id, lines, status, descr, who, created, updated, done, enter_op, loss_op, error"


def _row(r):
    i, store, lines, status, descr, who, created, updated, done, eop, lop, err = r
    return {"id": i, "store": store, "lines": json.loads(lines or "[]"), "status": status, "descr": descr or "", "who": who or "",
            "created": created or 0, "updated": updated or 0, "done": done or 0, "enterOp": eop or "", "lossOp": lop or "", "error": err or ""}


def get(inv_id):
    with db() as d:
        r = d.run(f"SELECT {_COLS} FROM wh_inv WHERE id=%s", (inv_id,), one=True)
        if not r:
            return None
        o = _row(r)
        o["storeName"] = (d.run("SELECT name FROM ms_store WHERE id=%s", (o["store"],), one=True) or [""])[0]
        if o["status"] == "draft":        # подсказка «по учёту» из нашей копии (точный учёт берётся из МойСклад при проведении)
            est = dict(d.run("SELECT product_id, stock FROM ms_stock WHERE store_id=%s", (o["store"],), many=True) or [])
            for x in o["lines"]:
                x["est"] = est.get(x["id"], 0) or 0
    for k in ("enterOp", "lossOp"):        # состояние документов-расхождений (очередь whops)
        op = whops.get(o[k]) if o[k] else None
        o[k[:-2]] = {"status": op["status"], "msNumber": op["msNumber"], "error": op["error"]} if op else None
    return o


def recent(limit=10):
    with db() as d:
        rows = d.run(f"SELECT {_COLS} FROM wh_inv ORDER BY created DESC LIMIT %s", (limit,), many=True) or []
        names = dict(d.run("SELECT id, name FROM ms_store", many=True) or [])
    out = []
    for r in rows:
        o = _row(r)
        o["storeName"] = names.get(o["store"], "")
        out.append(o)
    return out


def start(store_id, who=""):
    with db() as d:
        if not d.run("SELECT 1 FROM ms_store WHERE id=%s AND archived=0", (store_id,), one=True):
            raise ValueError("Выберите склад")
        inv_id, t = str(uuid.uuid4()), time.time()
        d.run(f"INSERT INTO wh_inv ({_COLS}) VALUES (%s, %s, '[]', 'draft', '', %s, %s, %s, 0, '', '', '')", (inv_id, store_id, who, t, t))
    return get(inv_id)


def save(inv_id, lines, descr=""):
    """Черновик: список {id, fact}. Названия и вид товара берутся из нашей копии каталога."""
    o = get(inv_id)
    if not o:
        raise LookupError("Инвентаризация не найдена")
    if o["status"] != "draft":
        raise ValueError("Инвентаризация уже проводится — изменить нельзя")
    clean, seen = [], set()
    with mirror.db() as d:
        for x in lines or []:
            pid = str(x.get("id") or "")
            if pid in seen:
                continue
            try:
                fact = float(x.get("fact") or 0)
            except (TypeError, ValueError):
                raise ValueError("Количество — число")
            if fact < 0:
                raise ValueError("Количество не может быть меньше нуля")
            p = d.run("SELECT kind, name, code FROM ms_product WHERE id=%s AND deleted=0", (pid,), one=True)
            if not p:
                raise ValueError("Товар не найден — обновите список")
            seen.add(pid)
            clean.append({"id": pid, "kind": p[0] or "product", "name": p[1], "code": p[2] or "", "fact": fact})
    with db() as d:
        d.run("UPDATE wh_inv SET lines=%s, descr=%s, updated=%s WHERE id=%s AND status='draft'",
              (json.dumps(clean, ensure_ascii=False), (descr or "").strip()[:300], time.time(), inv_id))
    return get(inv_id)


def delete(inv_id):
    with db() as d:
        d.run("DELETE FROM wh_inv WHERE id=%s AND status='draft'", (inv_id,))


def _book(store_id):
    """Учётные остатки склада из МойСклад прямо сейчас. Отчёт не содержит нулевых строк — нет строки = 0."""
    data = oh.ms("GET", "/report/stock/bystore/current", params={"stockType": "stock"}, timeout=60)
    rows = data if isinstance(data, list) else data.get("rows", [])
    return {r["assortmentId"]: r.get("stock", 0) or 0 for r in rows if r.get("storeId") == store_id}


def _op_id(inv_id, kind):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"amura-inventory/{inv_id}/{kind}"))


def conduct(inv_id, who=""):
    """Проводит расхождения. Повтор после сбоя безопасен: снимок учёта и id документов те же."""
    with _lock:
        o = get(inv_id)
        if not o:
            raise LookupError("Инвентаризация не найдена")
        if o["status"] == "done":
            return o
        if not o["lines"]:
            raise ValueError("Добавьте хотя бы один товар")
        lines = o["lines"]
        if o["status"] == "draft":           # первый раз: снимок учёта из МойСклад
            try:
                book = _book(o["store"])
            except Exception as e:
                raise ValueError("МойСклад не ответил — проведите чуть позже (" + str(e)[:120] + ")")
            for x in lines:
                x["book"] = book.get(x["id"], 0)
            with db() as d:
                d.run("UPDATE wh_inv SET lines=%s, status='conducting', updated=%s WHERE id=%s",
                      (json.dumps(lines, ensure_ascii=False), time.time(), inv_id))
        descr = "Инвентаризация" + (": " + o["descr"] if o["descr"] else "")
        ids = {}
        try:
            with mirror.db() as d:
                buy = {x["id"]: (d.run("SELECT buy_price FROM ms_product WHERE id=%s", (x["id"],), one=True) or [0])[0] or 0 for x in lines}
            for kind, sign in (("enter", 1), ("loss", -1)):
                pos = [{"id": x["id"], "qty": q, "price": buy[x["id"]]}
                       for x in lines for q in [round(sign * (x["fact"] - x["book"]), 3)] if q > 0]
                if pos:
                    ids[kind] = whops.create(kind, o["store"], pos, descr=descr, who=who or o["who"], op_id=_op_id(inv_id, kind))["id"]
        except ValueError as e:
            with db() as d:
                d.run("UPDATE wh_inv SET error=%s WHERE id=%s", (str(e)[:300], inv_id))
            raise
        with db() as d:
            d.run("UPDATE wh_inv SET status='done', done=%s, enter_op=%s, loss_op=%s, error='' WHERE id=%s",
                  (time.time(), ids.get("enter", ""), ids.get("loss", ""), inv_id))
    print(f"Склад: инвентаризация {inv_id[:8]} проведена: излишки {'да' if 'enter' in ids else 'нет'}, недостача {'да' if 'loss' in ids else 'нет'}", flush=True)
    return get(inv_id)
