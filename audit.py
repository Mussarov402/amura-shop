"""Контроль учёта (решение владельца 13.10.2026: «с этого момента ведём учёт нормально») — только чтение, в МойСклад ничего не пишет.

Проверки по нашей копии МойСклад (зеркало):
1) приёмки, новые или изменённые за сутки: позиция с ценой 0; закупка дешевле 20% розничной цены; цена сильно не как обычно
   (меньше 0,3 или больше 3 средней цены этого товара в прошлых приёмках); приёмка не в той валюте, в которой поставщик обычно работает;
2) отрицательные остатки (продали то, чего нет по учёту) — с днём, когда минус впервые замечен;
3) поставщики, которым мы должны и долг растёт (за 30 дней были приёмки), а оплат за 30 дней ни одной;
4) продажи за сутки с нулевой себестоимостью (в отчёте МойСклад и в карточке закупка 0).
Итог считается после сверки (ночью) и утром (в MORNING_HOUR по Алматы) уходит владельцу в Telegram — только если есть замечания и
включён переключатель «Контроль учёта» (Обзор → Уведомления, по умолчанию включён)."""
import json
import os
import threading
import time
from datetime import datetime, timedelta

import inbox
import mirror
import order_hook as oh

MORNING_HOUR = 9           # утреннее сообщение, Алматы
LOW_SHARE = 0.2            # закупка дешевле 20% розничной — подозрительно
AVG_LOW, AVG_HIGH = 0.3, 3  # цена не как обычно: меньше 0,3 или больше 3 средних прошлых
PAY_DAYS = 30              # без оплат поставщику столько дней
TOP = 10                   # строк каждого раздела в сообщении
SUPPLY = "supply"
OUT_PAY = ("paymentout", "cashout")
SALES = ("demand", "retaildemand")
_ready = [False]


def _schema(d):
    if _ready[0]:
        return
    pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    # audit_neg — когда минус по товару замечен впервые (день по Алматы); audit_run — итоги проверок
    d.run("CREATE TABLE IF NOT EXISTS audit_neg (product_id TEXT PRIMARY KEY, since TEXT)")
    d.run(f"CREATE TABLE IF NOT EXISTS audit_run (id {pk}, at DOUBLE PRECISION, body TEXT)")
    d.c.commit()
    _ready[0] = True


def db():
    d = mirror.db()
    _schema(d)
    return d


def _ms(dt):
    return dt.astimezone(mirror.MS_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _retail(prices_json):
    pr = json.loads(prices_json or "{}")
    return next((v for k, v in pr.items() if "розн" in k.lower()), 0) or 0


def _ph(seq):
    return ", ".join(["%s"] * len(seq))


def usual_cur(d, agent_id, exclude=""):
    """Валюта, в которой поставщик обычно работает (по большинству его приёмок): "" — тенге, "USD" — доллар; None — меньше двух приёмок."""
    rows = d.run("SELECT COALESCE(cur, ''), COUNT(*) FROM ms_doc WHERE type='supply' AND deleted=0 AND agent_id=%s AND id<>%s GROUP BY COALESCE(cur, '')",
                 (agent_id, exclude), many=True) or []
    if sum(r[1] for r in rows) < 2:                 # по одной приёмке «обычную» валюту не определить
        return None
    return max(rows, key=lambda r: r[1])[0]


def avg_buy(d, ids, exclude=""):
    """Средняя цена (₸) товара в прошлых проведённых приёмках, кроме документа exclude."""
    if not ids:
        return {}
    rows = d.run(f"SELECT l.product_id, SUM(l.sum), SUM(l.qty) FROM ms_doc_line l JOIN ms_doc x ON x.id=l.doc_id"
                 f" WHERE x.type='supply' AND x.deleted=0 AND x.applicable=1 AND x.id<>%s AND l.qty>0 AND l.price>0"
                 f" AND l.product_id IN ({_ph(ids)}) GROUP BY l.product_id", (exclude, *ids), many=True) or []
    return {p: s / q for p, s, q in rows if q}


def line_flags(price, retail, avg):
    """Замечания к цене позиции приёмки (всё в тенге)."""
    out = []
    if not price:
        out.append("цена 0")
    elif retail and price < LOW_SHARE * retail:
        out.append(f"закупка {round(price):,} ₸ — меньше 20% розничной {round(retail):,} ₸".replace(",", " "))
    if price and avg and (price < AVG_LOW * avg or price > AVG_HIGH * avg):
        out.append(f"обычно ~{round(avg):,} ₸".replace(",", " "))
    return out


def _supplies(d, since):
    out = []
    docs = d.run("SELECT x.id, x.number, x.moment, x.agent_id, a.name, COALESCE(x.cur, ''), x.applicable FROM ms_doc x"
                 " LEFT JOIN ms_agent a ON a.id=x.agent_id WHERE x.type='supply' AND x.deleted=0 AND (x.updated>=%s OR x.moment>=%s)"
                 " ORDER BY x.moment", (since, since), many=True) or []
    for i, num, moment, agent, name, cur, ap in docs:
        lines = d.run("SELECT l.product_id, p.name, p.prices, l.price, l.qty FROM ms_doc_line l LEFT JOIN ms_product p ON p.id=l.product_id"
                      " WHERE l.doc_id=%s", (i,), many=True) or []
        avg = avg_buy(d, [r[0] for r in lines if r[0]], exclude=i)
        items = []
        for pid, pname, prices, price, qty in lines:
            f = line_flags(price or 0, _retail(prices), avg.get(pid))
            if f:
                items.append({"product": pname or "(товар удалён)", "price": round(price or 0, 2), "qty": qty or 0, "flags": f})
        uc = usual_cur(d, agent, exclude=i) if agent else None
        cur_flag = ""
        if uc is not None and uc != cur:
            cur_flag = f"приёмка в {'$' if cur == 'USD' else cur or '₸'}, а поставщик обычно в {'$' if uc == 'USD' else uc or '₸'}"
        if items or cur_flag:
            out.append({"id": i, "number": num or "", "day": mirror._alm(moment)[:10], "agent": name or "", "curFlag": cur_flag,
                        "applicable": bool(ap), "lines": items})
    return out


def _negatives(d, today):
    rows = d.run("SELECT k.product_id, p.name, p.code, SUM(k.stock) FROM ms_stock k LEFT JOIN ms_product p ON p.id=k.product_id"
                 " GROUP BY k.product_id, p.name, p.code HAVING SUM(k.stock)<0 ORDER BY SUM(k.stock)", many=True) or []
    since = dict(d.run("SELECT product_id, since FROM audit_neg", many=True) or [])
    neg = {r[0] for r in rows}
    for pid in neg - set(since):
        d.run("INSERT INTO audit_neg (product_id, since) VALUES (%s, %s)", (pid, today))
        since[pid] = today
    for pid in set(since) - neg:                       # минус ушёл — забываем
        d.run("DELETE FROM audit_neg WHERE product_id=%s", (pid,))
    return [{"id": pid, "product": n or "(товар удалён)", "code": c or "", "stock": round(s, 3), "since": since.get(pid, today)}
            for pid, n, c, s in rows]


def _suppliers(d, now):
    a30 = _ms(now - timedelta(days=PAY_DAYS))
    rows = d.run("SELECT x.agent_id, a.name, COUNT(*), MAX(x.moment), b.balance FROM ms_doc x LEFT JOIN ms_agent a ON a.id=x.agent_id"
                 " LEFT JOIN ms_agent_balance b ON b.agent_id=x.agent_id WHERE x.type='supply' AND x.deleted=0 AND x.applicable=1"
                 " AND x.moment>=%s GROUP BY x.agent_id, a.name, b.balance", (a30,), many=True) or []
    out = []
    for agent, name, n, last, bal in rows:
        if not agent or (bal or 0) <= 0:              # плюс — мы должны поставщику
            continue
        paid = d.run(f"SELECT COUNT(*) FROM ms_doc WHERE agent_id=%s AND deleted=0 AND applicable=1 AND moment>=%s AND type IN ({_ph(OUT_PAY)})",
                     (agent, a30, *OUT_PAY), one=True)[0]
        if paid:
            continue
        total = d.run("SELECT COUNT(*) FROM ms_doc WHERE type='supply' AND deleted=0 AND applicable=1 AND agent_id=%s", (agent,), one=True)[0]
        out.append({"id": agent, "name": name or "", "balance": round(bal), "supplies30": n, "supplies": total, "lastSupply": mirror._alm(last)[:10]})
    return sorted(out, key=lambda r: -r["balance"])


def _zero_cost(d, since):
    rows = d.run(f"SELECT l.product_id, p.name, SUM(l.qty), COUNT(DISTINCT x.id) FROM ms_doc x JOIN ms_doc_line l ON l.doc_id=x.id"
                 f" LEFT JOIN ms_product p ON p.id=l.product_id LEFT JOIN ms_cost c ON c.product_id=l.product_id"
                 f" WHERE x.deleted=0 AND x.applicable=1 AND x.moment>=%s AND x.type IN ({_ph(SALES)})"
                 f" AND COALESCE(c.cost, 0)=0 AND COALESCE(p.buy_price, 0)=0 GROUP BY l.product_id, p.name ORDER BY SUM(l.qty) DESC",
                 (since, *SALES), many=True) or []
    return [{"id": pid, "product": n or "(товар удалён)", "qty": round(q or 0, 3), "docs": c} for pid, n, q, c in rows]


def check(now=None):
    """Все проверки сейчас (только чтение МойСклад-копии; пишет лишь свою таблицу дней появления минуса)."""
    now = now or datetime.now(oh.ALMATY)
    since = _ms(now - timedelta(days=1))
    with db() as d:
        res = {"at": time.time(), "supplies": _supplies(d, since), "negatives": _negatives(d, now.strftime("%Y-%m-%d")),
               "suppliers": _suppliers(d, now), "zeroCost": _zero_cost(d, since)}
    res["count"] = sum(len(res[k]) for k in ("supplies", "negatives", "suppliers", "zeroCost"))
    return res


def save(res):
    with db() as d:
        d.run("INSERT INTO audit_run (at, body) VALUES (%s, %s)", (res["at"], json.dumps(res, ensure_ascii=False)))
        d.run("DELETE FROM audit_run WHERE at<%s", (time.time() - 30 * 86400,))


def last():
    with db() as d:
        r = d.run("SELECT body FROM audit_run ORDER BY id DESC LIMIT 1", one=True)
    return json.loads(r[0]) if r else None


def nightly():
    """После ночной сверки: проверить и записать итог (в логи — коротко)."""
    res = check()
    save(res)
    print(f"Контроль учёта: приёмки с замечаниями {len(res['supplies'])}; в минусе {len(res['negatives'])} поз.;"
          f" поставщики без оплат {len(res['suppliers'])}; продажи без себестоимости {len(res['zeroCost'])} поз.", flush=True)
    return res


def text(res):
    """Сообщение владельцу (Telegram, до 4000 знаков)."""
    fmt = lambda v: f"{round(v):,}".replace(",", " ")
    parts = ["📋 Контроль учёта — замечания (в МойСклад ничего не менялось):"]
    if res["supplies"]:
        parts.append(f"\n1) Приёмки за сутки ({len(res['supplies'])}):")
        for s in res["supplies"][:TOP]:
            head = f"• №{s['number']} · {s['agent'] or 'без поставщика'} · {s['day']}"
            parts.append(head + (f" — {s['curFlag']}" if s["curFlag"] else ""))
            for ln in s["lines"][:5]:
                parts.append(f"   – {ln['product'][:50]}: {', '.join(ln['flags'])}")
            if len(s["lines"]) > 5:
                parts.append(f"   … и ещё {len(s['lines']) - 5} поз.")
    if res["negatives"]:
        parts.append(f"\n2) Отрицательные остатки ({len(res['negatives'])} поз.) — продано без приёмки:")
        for n in res["negatives"][:TOP]:
            parts.append(f"• {n['product'][:50]}: {n['stock']:g} шт. (с {n['since']})")
    if res["suppliers"]:
        parts.append(f"\n3) Поставщики без оплат за {PAY_DAYS} дней, долг растёт:")
        for s in res["suppliers"][:TOP]:
            parts.append(f"• {s['name'][:40]}: мы должны {fmt(s['balance'])} ₸, приёмок за 30 дней {s['supplies30']}, всего {s['supplies']}")
    if res["zeroCost"]:
        parts.append(f"\n4) Продажи за сутки с нулевой себестоимостью ({len(res['zeroCost'])} поз.):")
        for z in res["zeroCost"][:TOP]:
            parts.append(f"• {z['product'][:50]}: {z['qty']:g} шт.")
    parts.append("\nПодробно: система AMURA → Отчёты → «Контроль учёта».")
    t = "\n".join(parts)
    return t if len(t) <= 4000 else t[:3990] + "\n…"


def morning(force=False):
    """Утреннее сообщение владельцу — раз в день, только при замечаниях и включённом «Контроле учёта»."""
    today = datetime.now(oh.ALMATY).strftime("%Y-%m-%d")
    with inbox.db() as d:
        if not force and inbox.get_setting(d, "audit_sent_day", "") == today:
            return None
        inbox.set_setting(d, "audit_sent_day", today)
    res = check()
    save(res)
    if not res["count"] or not oh.notif_on("audit") or not oh.OWNER:
        return res
    try:
        oh.tg("sendMessage", chat_id=oh.OWNER, text=text(res))
    except Exception as e:
        print("Контроль учёта: сообщение не отправлено:", e, flush=True)
    return res


def _loop():
    time.sleep(200)
    while True:
        try:
            if mirror.enabled() and datetime.now(oh.ALMATY).hour >= MORNING_HOUR:
                morning()
        except Exception as e:
            print("Контроль учёта (цикл):", e, flush=True)
        time.sleep(600)


if os.environ.get("PORT"):
    threading.Thread(target=_loop, daemon=True).start()
