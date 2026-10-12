"""Этикетки и ценники со штрихкодом в PDF (по нашей копии карточек товаров, только чтение).

- kind=label — этикетка: название, штрихкод, код товара;
- kind=price — ценник: название, крупная цена (выбранный тип цены, по умолчанию розничная), штрихкод;
- size=58x40 — термопринтер, одна этикетка на страницу 58×40 мм; size=a4 — лист A4, 3 × 8 этикеток 70 × 37 мм;
- штрихкод — первый из карточки: 13 цифр с верной контрольной суммой — EAN-13, иначе Code128; нет штрихкода — Code128 по коду товара;
- количество каждой этикетки задаётся (например, из приёмки — сколько пришло), всего не больше MAX_LABELS."""
import io
import json

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

import mirror
import order_hook as oh  # noqa: F401  (регистрирует шрифты DejaVu с кириллицей и знаком ₸)

MAX_LABELS = 1000
SIZES = {"58x40": (58 * mm, 40 * mm), "a4": (70 * mm, A4[1] / 8)}


def _ean_ok(s):
    if len(s) != 13 or not s.isdigit():
        return False
    total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(s[:12]))
    return (10 - total % 10) % 10 == int(s[12])


def price_types(ids):
    """Типы цен, которые есть у этих товаров (для выбора на ценнике)."""
    ids = [i for i in dict.fromkeys(ids) if i]
    names = []
    if ids:
        with mirror.db() as d:
            for (pr,) in d.run("SELECT prices FROM ms_product WHERE id IN (" + ", ".join(["%s"] * len(ids)) + ")", ids, many=True) or []:
                for k in json.loads(pr or "{}"):
                    if k not in names:
                        names.append(k)
    return sorted(names, key=lambda k: ("розн" not in k.lower(), k))


def products(ids, price_type=""):
    """Данные для этикеток: название, код, штрихкод, цена выбранного типа (по умолчанию розничная, иначе первая)."""
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    with mirror.db() as d:
        rows = d.run("SELECT id, name, code, prices, barcodes FROM ms_product WHERE deleted=0 AND id IN (" + ", ".join(["%s"] * len(ids)) + ")",
                     ids, many=True) or []
    out = {}
    for i, n, c, pr, bc in rows:
        prices = json.loads(pr or "{}")
        pt = price_type if price_type in prices else next((k for k in prices if "розн" in k.lower()), next(iter(prices), ""))
        codes = json.loads(bc or "[]")
        out[i] = {"name": n or "", "code": c or "", "barcode": codes[0] if codes else "", "price": round(prices.get(pt) or 0)}
    return out


def _wrap(c, text, font, size, width, lines):
    words, out, cur = (text or "").split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if c.stringWidth(t, font, size) <= width:
            cur = t
            continue
        if cur:
            out.append(cur)
        cur = w
        while c.stringWidth(cur, font, size) > width and len(cur) > 1:   # очень длинное слово — режем
            cut = len(cur)
            while cut > 1 and c.stringWidth(cur[:cut], font, size) > width:
                cut -= 1
            out.append(cur[:cut])
            cur = cur[cut:]
    if cur:
        out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        while out[-1] and c.stringWidth(out[-1] + "…", font, size) > width:
            out[-1] = out[-1][:-1]
        out[-1] += "…"
    return out


def _barcode(c, value, x, y, w, h):
    value = (value or "").strip()
    if not value:
        return
    if _ean_ok(value):
        dr = createBarcodeDrawing("EAN13", value=value[:12], barHeight=h, humanReadable=True, fontName="DV", fontSize=7)
    else:
        dr = createBarcodeDrawing("Code128", value=value, barHeight=h * 0.8, humanReadable=True, fontName="DV", fontSize=7,
                                  barWidth=0.33 * mm)
    sc = min(w / dr.width, 1.6)
    dr.scale(sc, 1)
    dr.width, dr.height = dr.width * sc, dr.height
    renderPDF.draw(dr, c, x + (w - dr.width) / 2, y)


def _one(c, p, kind, x, y, w, h):
    pad = 2.5 * mm
    iw = w - 2 * pad
    if kind == "price":
        lines = _wrap(c, p["name"], "DV", 8, iw, 2)
        ty = y + h - pad - 8
        for ln in lines:
            c.setFont("DV", 8)
            c.drawString(x + pad, ty, ln)
            ty -= 9.5
        price = f"{p['price']:,}".replace(",", " ") + " ₸" if p["price"] else "цена не указана"
        fs = 20 if p["price"] else 10
        while fs > 10 and c.stringWidth(price, "DVB", fs) > iw:
            fs -= 1
        c.setFont("DVB", fs)
        c.drawCentredString(x + w / 2, ty - fs + 4, price)
        _barcode(c, p["barcode"] or p["code"], x + pad, y + pad, iw, 7 * mm)
    else:
        lines = _wrap(c, p["name"], "DVB", 8, iw, 3)
        ty = y + h - pad - 8
        for ln in lines:
            c.setFont("DVB", 8)
            c.drawString(x + pad, ty, ln)
            ty -= 9.5
        _barcode(c, p["barcode"] or p["code"], x + pad, y + pad + 7, iw, 11 * mm)
        if p["code"]:
            c.setFont("DV", 6.5)
            c.drawString(x + pad, y + pad - 1, "код " + p["code"])


def pdf(items, kind="label", size="58x40", price_type=""):
    """items — [{"id", "qty"}]. Возвращает байты PDF. Ошибка (нет товаров, слишком много) — ValueError."""
    kind = "price" if kind == "price" else "label"
    size = size if size in SIZES else "58x40"
    qty = {}
    for x in items or []:
        try:
            n = int(x.get("qty") or 1)
        except (TypeError, ValueError):
            n = 1
        if n > 0:
            qty[str(x.get("id") or "")] = qty.get(str(x.get("id") or ""), 0) + n
    data = products(list(qty), price_type)
    seq = [data[i] for i in qty if i in data for _ in range(qty[i])]
    if not seq:
        raise ValueError("Нет товаров для печати")
    if len(seq) > MAX_LABELS:
        raise ValueError(f"Слишком много этикеток за раз (больше {MAX_LABELS})")
    buf = io.BytesIO()
    w, h = SIZES[size]
    if size == "58x40":
        c = canvas.Canvas(buf, pagesize=(w, h))
        for p in seq:
            _one(c, p, kind, 0, 0, w, h)
            c.showPage()
    else:
        c = canvas.Canvas(buf, pagesize=A4)
        cols, rows = 3, 8
        mx = (A4[0] - cols * w) / 2
        for i, p in enumerate(seq):
            k = i % (cols * rows)
            if i and not k:
                c.showPage()
            col, row = k % cols, k // cols
            _one(c, p, kind, mx + col * w, A4[1] - (row + 1) * h, w, h)
        c.showPage()
    c.setTitle("AMURA — " + ("ценники" if kind == "price" else "этикетки"))
    c.save()
    return buf.getvalue()
