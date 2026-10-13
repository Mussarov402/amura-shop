"""Отчёты по нашей копии МойСклад (только чтение): прибыль по товарам и клиентам, обороты товаров.

- продажи — проведённые отгрузки и чеки минус возвраты покупателей и по чекам (позиции документов, сумма со скидкой);
- себестоимость — количество × закупочная цена из карточки товара (приближение: МойСклад считает по партиям,
  поэтому прибыль здесь может немного отличаться от его отчёта «Прибыльность»);
- период — по дате документа в Алматы, не больше окна зеркала (DOC_DAYS);
- обороты — приход (приёмки, оприходования, возвраты покупателей) и расход (отгрузки, чеки, списания, возвраты поставщикам)
  по товарам за период и текущий остаток."""
from datetime import datetime, timedelta

import mirror
import order_hook as oh

SALE = ("demand", "retaildemand")
RET = ("salesreturn", "retailsalesreturn")
IN_DOCS = ("supply", "enter", "salesreturn", "retailsalesreturn")
OUT_DOCS = ("demand", "retaildemand", "loss", "purchasereturn")


def _bounds(days):
    """Последние days дней по Алматы (сегодня включительно) → границы moment в московском времени МойСклад."""
    days = max(1, min(int(days or 30), mirror.DOC_DAYS))
    today = datetime.now(oh.ALMATY).date()
    start = datetime(*(today - timedelta(days=days - 1)).timetuple()[:3], tzinfo=oh.ALMATY)
    end = datetime(*(today + timedelta(days=1)).timetuple()[:3], tzinfo=oh.ALMATY)
    f = lambda t: t.astimezone(mirror.MS_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return days, f(start), f(end)


def _ph(seq):
    return ", ".join(["%s"] * len(seq))


def profit(days=30, by="product", limit=50):
    """Прибыль за период по товарам (by=product) или клиентам (by=agent): выручка, себестоимость, прибыль, маржа."""
    days, a, b = _bounds(days)
    key = "l.product_id" if by == "product" else "x.agent_id"
    agg = {}
    with mirror.db() as d:
        for k, t, q, sm, buy in d.run(f"SELECT {key}, x.type, l.qty, l.sum, COALESCE(p.buy_price, 0)"
                                      f" FROM ms_doc x JOIN ms_doc_line l ON l.doc_id=x.id LEFT JOIN ms_product p ON p.id=l.product_id"
                                      f" WHERE x.deleted=0 AND x.applicable=1 AND x.moment>=%s AND x.moment<%s AND x.type IN ({_ph(SALE + RET)})",
                                      (a, b, *SALE, *RET), many=True) or []:
            sg = -1 if t in RET else 1
            r = agg.setdefault(k, [0, 0, 0])
            r[0] += sg * (q or 0)
            r[1] += sg * (sm or 0)
            r[2] += sg * (q or 0) * (buy or 0)
        rows = [(k, *v) for k, v in agg.items()]
        ids = [r[0] for r in rows if r[0]]
        if by == "product":
            names = {i: (n, c) for i, n, c in d.run(f"SELECT id, name, code FROM ms_product WHERE id IN ({_ph(ids)})", ids, many=True) or []} if ids else {}
        else:
            names = {i: (n, "") for i, n in d.run(f"SELECT id, name FROM ms_agent WHERE id IN ({_ph(ids)})", ids, many=True) or []} if ids else {}
    out = []
    for k, q, rev, cost in rows:
        rev, cost = round(rev or 0), round(cost or 0)
        n, c = names.get(k, ("(удалён)" if k else "Без контрагента", ""))
        out.append({"id": k or "", "name": n or "—", "code": c or "", "qty": round(q or 0, 3), "revenue": rev, "cost": cost,
                    "profit": rev - cost, "margin": round((rev - cost) * 100 / rev, 1) if rev else 0})
    out.sort(key=lambda r: -r["revenue"])
    tot = {k: sum(r[k] for r in out) for k in ("revenue", "cost", "profit")}
    tot["margin"] = round(tot["profit"] * 100 / tot["revenue"], 1) if tot["revenue"] else 0
    tot["noCost"] = sum(1 for r in out if r["revenue"] and not r["cost"]) if by == "product" else 0
    return {"days": days, "by": by, "total": tot, "count": len(out), "rows": out[:limit]}


def turnover(days=30, q="", limit=50):
    """Обороты товаров за период: приход, расход (шт.) и текущий остаток — по всем складам."""
    days, a, b = _bounds(days)
    q = (q or "").strip()
    with mirror.db() as d:
        rows = d.run(f"SELECT l.product_id, SUM(CASE WHEN x.type IN ({_ph(IN_DOCS)}) THEN l.qty ELSE 0 END),"
                     f" SUM(CASE WHEN x.type IN ({_ph(OUT_DOCS)}) THEN l.qty ELSE 0 END)"
                     f" FROM ms_doc x JOIN ms_doc_line l ON l.doc_id=x.id"
                     f" WHERE x.deleted=0 AND x.applicable=1 AND x.moment>=%s AND x.moment<%s AND x.type IN ({_ph(IN_DOCS + OUT_DOCS)})"
                     f" GROUP BY l.product_id", (*IN_DOCS, *OUT_DOCS, a, b, *IN_DOCS, *OUT_DOCS), many=True) or []
        ids = [r[0] for r in rows if r[0]]
        info = {i: (n, c) for i, n, c in d.run(f"SELECT id, name, code FROM ms_product WHERE id IN ({_ph(ids)})", ids, many=True) or []} if ids else {}
        stock = dict(d.run(f"SELECT product_id, SUM(stock) FROM ms_stock WHERE product_id IN ({_ph(ids)}) GROUP BY product_id", ids, many=True) or []) if ids else {}
    out = []
    for pid, i, o in rows:
        n, c = info.get(pid, ("(удалён)", ""))
        if q and q.lower() not in (n or "").lower() and q not in (c or ""):
            continue
        out.append({"id": pid, "name": n or "—", "code": c or "", "in": round(i or 0, 3), "out": round(o or 0, 3), "stock": round(stock.get(pid, 0) or 0, 3)})
    out.sort(key=lambda r: -(r["out"] + r["in"]))
    return {"days": days, "count": len(out), "totalIn": round(sum(r["in"] for r in out), 3), "totalOut": round(sum(r["out"] for r in out), 3),
            "rows": out[:limit]}


def xlsx(kind="product", days=30, q=""):
    """Отчёт целиком (без ограничения строк) в Excel: прибыль по товарам / клиентам или обороты."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    if kind == "turn":
        r = turnover(days, q, limit=100000)
        ws.title = "Обороты"
        head = ["Товар", "Код", "Приход, шт.", "Расход, шт.", "Остаток, шт."]
        rows = [[x["name"], x["code"], x["in"], x["out"], x["stock"]] for x in r["rows"]]
        total = ["Итого", "", r["totalIn"], r["totalOut"], ""]
    else:
        by = "agent" if kind == "agent" else "product"
        r = profit(days, by, limit=100000)
        ws.title = "Прибыль по клиентам" if by == "agent" else "Прибыль по товарам"
        head = (["Клиент"] if by == "agent" else ["Товар", "Код", "Кол-во, шт."]) + ["Выручка, ₸", "Себестоимость, ₸", "Прибыль, ₸", "Маржа, %"]
        rows = [([x["name"]] if by == "agent" else [x["name"], x["code"], x["qty"]]) + [x["revenue"], x["cost"], x["profit"], x["margin"]]
                for x in r["rows"]]
        t = r["total"]
        total = (["Итого"] if by == "agent" else ["Итого", "", ""]) + [t["revenue"], t["cost"], t["profit"], t["margin"]]
    ws.append([f"AMURA — {ws.title} за {r['days']} дн. (по данным МойСклад в системе AMURA)"])
    ws["A1"].font = Font(bold=True)
    ws.append(head)
    for c in ws[2]:
        c.font = Font(bold=True)
    for row in rows:
        ws.append(row)
    ws.append(total)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    ws.column_dimensions["A"].width = 48
    for col in "BCDEFG":
        ws.column_dimensions[col].width = 16
    ws.freeze_panes = "A3"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
