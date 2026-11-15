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
    m = re.search(r"\+?\d[\d\s()-]{9,}\d", who or "")
    phone = (m.group(0).strip() if m else "") or str(agent.get("phone") or "").strip()      # как в заказе, без переформатирования
    ship = line("Отправка:")
    method, _, addr = ship.partition(" — ")
    track = line("Трек:") or line("Трек-номер:")
    goods = [p for p in (positions or []) if ((p.get("assortment") or {}).get("meta") or {}).get("type") != "service"]
    total, paid = (o.get("sum") or 0) / 100, (o.get("payedSum") or 0) / 100
    return {"number": str(o.get("name") or ""), "name": name, "phone": phone, "city": line("Город:"),
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


def _fit(c, parts, width, height, big, small_min):
    """Подобрать размер шрифта, чтобы все строки (без обрезки) влезли в высоту: parts — [(текст, жирный)]."""
    size = big
    while True:
        lines = []
        for t, bold in parts:
            if t:
                lines += [(ln, bold) for ln in simpleSplit(str(t), "DVB" if bold else "DV", size, width)]
        if len(lines) * size * 1.18 <= height or size <= small_min:
            return size, lines
        size -= 0.5


def _block(c, x, y, size, lines):
    for ln, bold in lines:
        c.setFont("DVB" if bold else "DV", size)
        c.drawString(x, y - size, ln)
        y -= size * 1.18
    return y


def short_method(m):
    m = str(m or "")
    if "СДЭК" in m:
        return "СДЭК ПВЗ" if ("пункт" in m.lower() or "пвз" in m.lower()) else "СДЭК"
    return m


def pdf(items, fmt="t75", sender_phone="", barcode=False):
    """Адрес — целиком, шрифт ужимается, без обрезки. Штрихкод номера — только по галочке (владелец 15.11.2026: «не сканируем»)."""
    W, H = SIZES.get(fmt, SIZES["t75"])
    small = fmt == "t58"
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W * mm, H * mm))
    c.setTitle("Этикетки заказов AMURA")
    pad = (2 if small else 4) * mm
    width = W * mm - 2 * pad
    for i, x in enumerate(items):
        if i:
            c.showPage()
        top = H * mm - pad
        if small:
            c.setFont("DVB", 14)
            c.drawString(pad, top - 12, "№" + x["number"])
            y = top - 15
            if barcode:
                _code(c, x["number"], pad, y - 6 * mm, width, 5.5 * mm)
                y -= 6.5 * mm
            foot = short_method(x["method"])
            bottom = pad + (7 if foot else 0)
            size, lines = _fit(c, [(x["name"], True), (x["phone"], False), (", ".join(v for v in (x["city"], x["addr"]) if v), False)],
                               width, y - bottom, 9, 4.5)
            _block(c, pad, y, size, lines)
            if foot:
                c.setFont("DVB", 6.5)
                c.drawString(pad, pad, foot[:60])
            continue
        c.setFont("DVB", 10)
        c.drawString(pad, top - 10, "AMURA")
        if x.get("paid") is not None:
            lab = "ОПЛАЧЕН" if x["paid"] else "НЕ ОПЛАЧЕН"
            lw = c.stringWidth(lab, "DVB", 9) + 3 * mm
            c.setLineWidth(0.8)
            c.rect(W * mm - pad - lw, top - 12, lw, 13, fill=1 if x["paid"] else 0)
            c.setFillGray(1 if x["paid"] else 0)
            c.setFont("DVB", 9)
            c.drawString(W * mm - pad - lw + 1.5 * mm, top - 9, lab)
            c.setFillGray(0)
        y = top - 6 * mm
        c.setFont("DVB", 28)
        c.drawString(pad, y - 26, "№" + x["number"])
        y -= 32
        if barcode:
            _code(c, x["number"], pad, y - 13 * mm, width, 12 * mm)
            y -= 15 * mm
        c.setLineWidth(0.5)
        c.line(pad, y, W * mm - pad, y)
        y -= 2 * mm
        c.setFont("DV", 7.5)
        c.drawString(pad, y - 7.5, "Получатель")
        y -= 10
        info = " · ".join(v for v in ((f"Позиций: {x['lines']} · штук: {x['qty']}" if x["lines"] else ""),) if v)
        tail = [(("Доставка: " + x["method"]) if x["method"] else "", False), (("Трек: " + x["track"]) if x["track"] else "", True), (info, False)]
        bottom = pad + 12 * mm
        size, lines = _fit(c, [(x["name"], True), (x["phone"], True), (x["city"], True), (x["addr"], False)] + tail, width, y - bottom, 13, 6)
        _block(c, pad, y, size, lines)
        c.line(pad, pad + 9 * mm, W * mm - pad, pad + 9 * mm)
        c.setFont("DV", 8)
        c.drawString(pad, pad + 5 * mm, "Отправитель")
        c.setFont("DVB", 10)
        c.drawString(pad, pad + 1 * mm, "AMURA" + (f" · {sender_phone}" if sender_phone else ""))
    c.save()
    return buf.getvalue()
