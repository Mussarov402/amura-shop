"""Адресная этикетка своих заказов (розница amura.kz и опт) на термопринтер — владелец 13.11.2026.

Размеры как у площадок: 75×120 мм (как Ozon, по умолчанию) и 58×40 мм (как WB), одна этикетка на страницу, без полей.
75×120: номер заказа крупно + Code128, получатель (ФИО, телефон), город и адрес / пункт СДЭК / курьер, способ доставки и трек
(если есть), позиций и штук, «Оплачен» / «Не оплачен», отправитель AMURA и телефон магазина.
58×40: номер + штрихкод, ФИО, телефон, город, способ доставки. Нет поля — строку не печатаем.
Данные — из заказа МойСклад (контрагент, описание «Город: … / Отправка: …», оплата, позиции)."""
import io
import re

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

import order_hook as oh

SIZES = {"t75": (75, 120), "t58": (58, 40)}
MAX = 200


def _phone(v):
    d = re.sub(r"\D", "", str(v or ""))
    if len(d) == 11 and d[0] in "78":
        return f"+7 {d[1:4]} {d[4:7]} {d[7:9]} {d[9:11]}"
    return str(v or "").strip()


def parse(o, positions=None):
    """Заказ МойСклад → поля этикетки. positions — позиции (для штук); услуги и доставка не считаются."""
    desc = (o.get("description") or "").split("\n")
    line = lambda pfx: next((x[len(pfx):].strip() for x in desc if x.startswith(pfx)), "")   # noqa: E731
    who = desc[1] if len(desc) > 1 and not desc[1].startswith(("Город:", "Отправка:")) else ""
    agent = o.get("agent") or {}
    name = (who.split(",")[0].strip() if who else "") or str(agent.get("name") or "").strip()
    phone = agent.get("phone") or (re.search(r"\+?\d[\d\s()-]{9,}", who).group(0) if who and re.search(r"\+?\d[\d\s()-]{9,}", who) else "")
    ship = line("Отправка:")
    method, _, addr = ship.partition(" — ")
    track = line("Трек:") or line("Трек-номер:")
    goods = [p for p in (positions or []) if ((p.get("assortment") or {}).get("meta") or {}).get("type") != "service"]
    total, paid = (o.get("sum") or 0) / 100, (o.get("payedSum") or 0) / 100
    return {"number": str(o.get("name") or ""), "name": name, "phone": _phone(phone), "city": line("Город:"),
            "method": method.strip(), "addr": addr.strip(), "track": track,
            "lines": len(goods), "qty": int(sum(p.get("quantity") or 0 for p in goods)),
            "paid": None if not total else paid >= total - 0.5}


def load(numbers):
    out = []
    for n in [str(x).strip() for x in numbers if str(x).strip()][:MAX]:
        rows = oh.ms("GET", "/entity/customerorder", params={"filter": f"name={n}", "limit": 1, "expand": "agent"}, timeout=15).get("rows") or []
        if not rows:
            continue
        o = rows[0]
        out.append(parse(o, oh.order_positions(o["id"])))
    return out


def shop_phone():
    try:
        import delivery
        return _phone(delivery.conf()["store"].get("wh_phone") or "")
    except Exception:
        return ""


def _code(c, value, x, y, w, h):
    dr = createBarcodeDrawing("Code128", value=value, barHeight=h, humanReadable=False, barWidth=0.4 * mm)
    sc = min(w / dr.width, 2.2)
    dr.scale(sc, 1)
    dr.width = dr.width * sc
    renderPDF.draw(dr, c, x + (w - dr.width) / 2, y)


def pdf(items, fmt="t75", sender_phone=""):
    W, H = SIZES.get(fmt, SIZES["t75"])
    small = fmt == "t58"
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W * mm, H * mm))
    c.setTitle("Этикетки заказов AMURA")
    pad = (2.5 if small else 4) * mm
    width = W * mm - 2 * pad
    for i, x in enumerate(items):
        if i:
            c.showPage()
        y = H * mm - pad

        def text(t, font="DV", size=9, maxl=1, gap=1.25):
            nonlocal y
            if not t:
                return
            for ln in simpleSplit(str(t), font, size, width)[:maxl]:
                c.setFont(font, size)
                c.drawString(pad, y - size, ln)
                y -= size * gap

        num = "№" + x["number"]
        if small:
            text(num, "DVB", 15, gap=1.15)
            _code(c, x["number"], pad, y - 8 * mm, width, 7 * mm)
            y -= 9 * mm
            text(x["name"], "DVB", 8)
            text(x["phone"], "DV", 8)
            text(" · ".join(v for v in (x["city"], x["method"]) if v), "DV", 7, maxl=2, gap=1.2)
            continue
        c.setFont("DVB", 10)
        c.drawString(pad, y - 10, "AMURA")
        if x.get("paid") is not None:
            lab = "ОПЛАЧЕН" if x["paid"] else "НЕ ОПЛАЧЕН"
            lw = c.stringWidth(lab, "DVB", 9) + 3 * mm
            c.setLineWidth(0.8)
            c.rect(W * mm - pad - lw, y - 12, lw, 13, fill=1 if x["paid"] else 0)
            c.setFillGray(1 if x["paid"] else 0)
            c.setFont("DVB", 9)
            c.drawString(W * mm - pad - lw + 1.5 * mm, y - 9, lab)
            c.setFillGray(0)
        y -= 6 * mm
        text(num, "DVB", 30, gap=1.05)
        _code(c, x["number"], pad, y - 15 * mm, width, 14 * mm)
        y -= 18 * mm
        c.setLineWidth(0.5)
        c.line(pad, y, W * mm - pad, y)
        y -= 2.5 * mm
        text("Получатель", "DV", 7.5)
        text(x["name"], "DVB", 13, maxl=2, gap=1.15)
        text(x["phone"], "DVB", 12)
        y -= 1.5 * mm
        text(x["city"], "DVB", 11)
        text(x["addr"], "DV", 10, maxl=4)
        y -= 1.5 * mm
        text(("Доставка: " + x["method"]) if x["method"] else "", "DV", 10, maxl=2)
        text(("Трек: " + x["track"]) if x["track"] else "", "DVB", 10)
        if x["lines"]:
            text(f"Позиций: {x['lines']} · штук: {x['qty']}", "DV", 10)
        c.line(pad, pad + 9 * mm, W * mm - pad, pad + 9 * mm)
        c.setFont("DV", 8)
        c.drawString(pad, pad + 5 * mm, "Отправитель")
        c.setFont("DVB", 10)
        c.drawString(pad, pad + 1 * mm, "AMURA" + (f" · {sender_phone}" if sender_phone else ""))
    c.save()
    return buf.getvalue()
