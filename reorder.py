"""Подсказка «что дозаказать» по нашей копии МойСклад (только чтение).

Скорость продаж — проданное за последние `days` дней (отгрузки и чеки минус возвраты) / days.
Дозаказать — товары, где остатка меньше, чем на `cover` дней продаж; сколько — до запаса на `target` дней.
Поставщик — из последней проведённой приёмки этого товара (в окне зеркала), цена — закупочная из карточки."""
import math

import mirror
import reports

SALE, RET = reports.SALE, reports.RET


def suggest(days=28, cover=14, target=30, q=""):
    days = max(7, min(int(days or 28), mirror.DOC_DAYS))
    cover, target = max(1, int(cover or 14)), max(1, int(target or 30))
    target = max(target, cover)
    _, a, b = reports._bounds(days)
    sold = {}
    with mirror.db() as d:
        for pid, t, qty in d.run(f"SELECT l.product_id, x.type, l.qty FROM ms_doc x JOIN ms_doc_line l ON l.doc_id=x.id"
                                 f" WHERE x.deleted=0 AND x.applicable=1 AND x.moment>=%s AND x.moment<%s AND x.type IN ({reports._ph(SALE + RET)})",
                                 (a, b, *SALE, *RET), many=True) or []:
            sold[pid] = sold.get(pid, 0) + (-(qty or 0) if t in RET else (qty or 0))
        ids = [p for p, s in sold.items() if p and s > 0]
        if not ids:
            return {"days": days, "cover": cover, "target": target, "rows": [], "suppliers": []}
        ph = reports._ph(ids)
        info = {i: (n, c, bp) for i, n, c, bp in d.run(f"SELECT id, name, code, buy_price FROM ms_product WHERE deleted=0 AND archived=0 AND id IN ({ph})", ids, many=True) or []}
        stock = dict(d.run(f"SELECT product_id, SUM(stock) FROM ms_stock WHERE product_id IN ({ph}) GROUP BY product_id", ids, many=True) or [])
        last = {}
        for pid, agent, name, moment in d.run(f"SELECT l.product_id, x.agent_id, a.name, x.moment FROM ms_doc x JOIN ms_doc_line l ON l.doc_id=x.id"
                                              f" LEFT JOIN ms_agent a ON a.id=x.agent_id WHERE x.type='supply' AND x.deleted=0 AND x.applicable=1"
                                              f" AND l.product_id IN ({ph}) ORDER BY x.moment", ids, many=True) or []:
            last[pid] = (agent or "", name or "")                # по возрастанию даты — остаётся последняя приёмка
    rows = []
    for pid in ids:
        if pid not in info:
            continue
        n, c, bp = info[pid]
        rate = sold[pid] / days
        st = max(0, stock.get(pid, 0) or 0)
        if st >= rate * cover:
            continue
        need = math.ceil(rate * target - st)
        if need <= 0:
            continue
        ag, an = last.get(pid, ("", ""))
        q_ = (q or "").strip().lower()
        if q_ and q_ not in (n or "").lower() and q_ not in (c or "") and q_ not in an.lower():
            continue
        rows.append({"id": pid, "name": n or "—", "code": c or "", "stock": round(st, 3), "sold": round(sold[pid], 3),
                     "perDay": round(rate, 2), "daysLeft": round(st / rate, 1) if rate else None, "qty": need,
                     "price": round(bp or 0), "agent": ag, "agentName": an})
    rows.sort(key=lambda r: (r["daysLeft"] if r["daysLeft"] is not None else 0, -r["perDay"]))
    sup = {}
    for r in rows:
        s = sup.setdefault(r["agent"], {"agent": r["agent"], "name": r["agentName"] or "Поставщик не известен", "count": 0, "sum": 0})
        s["count"] += 1
        s["sum"] += r["qty"] * r["price"]
    return {"days": days, "cover": cover, "target": target, "rows": rows,
            "suppliers": sorted(sup.values(), key=lambda s: -s["sum"])}
