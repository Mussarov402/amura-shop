"""«Сборка» в разделе «Заказы» (модуль «Каналы продаж», выбор владельца 04.11.2026): сборочный лист WB и Ozon, подтверждение
сборки на площадке, этикетки одним файлом, поставка WB с QR.

- Новые: Ozon — отправления «awaiting_packaging»; WB — /api/v3/orders/new (ещё не в поставке).
- Собранные (для печати этикеток): Ozon — «awaiting_deliver»; WB — задания открытой поставки.
- Подтвердить сборку: Ozon /v4/posting/fbs/ship одной коробкой; WB — в открытую поставку (нет — создаём «AMURA дд.мм»).
  Ошибка (например, нужна маркировка) — у заказа, остальные подтверждаются.
- Этикетки одним PDF в порядке сборочного листа: Ozon — их PDF 75×120 (по 20 отправлений за запрос, склеиваем);
  WB — стикеры png 58×40, по странице на стикер.
- WB: «Закрыть поставку» — передать в доставку (/deliver) и QR поставки (/barcode, png → PDF).
Отгрузка МойСклад — как раньше (marketplaces.sync_orders: при передаче в доставку)."""
import base64
import io
import time
from datetime import datetime

from flask import Response, jsonify, request

import marketplaces as mpk
import order_hook as oh

_err = {}                                               # (магазин, номер) → ошибка последнего подтверждения (показываем у заказа)
WB_SUPPLY_PREFIX = "AMURA"


def shop(key):
    ch = {c.key: c for c in mpk.channels()}.get(str(key or ""))
    if not ch or not ch.keys_set():
        raise ValueError("Магазин не найден или на Render не вписан ключ")
    return ch


# ---------- списки ----------
def wb_open_supply(ch):
    nxt, best = 0, None
    for _ in range(20):
        j = ch.req("GET", "marketplace", "/api/v3/supplies", params={"limit": 1000, "next": nxt})
        for s in j.get("supplies") or []:
            if not s.get("done") and (best is None or str(s.get("createdAt", "")) > str(best.get("createdAt", ""))):
                best = s
        nxt = j.get("next") or 0
        if len(j.get("supplies") or []) < 1000 or not nxt:
            break
    return best


def lists(ch):
    """→ (новые, собранные, поставка WB или None): заказы в виде marketplaces.norm."""
    now = time.time()
    if isinstance(ch, mpk.WB):
        new = [ch.norm(o, {"supplierStatus": "new"}) for o in ch.req("GET", "marketplace", "/api/v3/orders/new").get("orders") or []]
        sup, done = wb_open_supply(ch), []
        if sup:
            ids = [o for o in ch.req("GET", "marketplace", f"/api/v3/supplies/{sup['id']}/orders").get("orders") or []]
            done = [ch.norm(o, {"supplierStatus": "confirm"}) for o in ids]
        return new, done, sup
    allp = ch.fetch(now - mpk.LOOKBACK_DAYS * 86400, now + 60)
    return ([o for o in allp if o["raw"] == "awaiting_packaging"], [o for o in allp if o["raw"] == "awaiting_deliver"], None)


def view(ch, orders, mp, cat):
    rows, items = [], {}
    for o in sorted(orders, key=lambda x: (x["cutoff"] or 9e12, x["at"])):
        rec = mpk.get(ch, o["ext"]) or {}
        lines = []
        for l in o["lines"]:
            mid = mpk.ms_id_for(l, mp)
            c = cat.get(mid) or {}
            key = mid or ("offer:" + (l["offer"] or l["ref"]))
            it = items.setdefault(key, {"id": mid or "", "code": c.get("code") or l["offer"], "name": c.get("name") or l["name"],
                                        "img": c.get("img") or "", "qty": 0, "orders": 0, "stock": c.get("qty")})
            it["qty"] += l["qty"]
            it["orders"] += 1
            lines.append({"code": it["code"], "name": it["name"], "img": it["img"], "qty": l["qty"], "pair": bool(mid)})
        rows.append({"ext": o["ext"], "label": o["label"], "number": rec.get("number") or "", "lines": lines,
                     "cutoff": o["cutoff"], "at": o["at"], "error": _err.get((ch.key, o["ext"]), "")})
    return rows, sorted(items.values(), key=lambda x: (str(x["code"]), x["name"]))


def route_list():
    _, err = mpk._me()
    if err:
        return err
    shops = [{"id": c.key, "title": c.title, "kind": "wb" if isinstance(c, mpk.WB) else "ozon"} for c in mpk.channels() if c.keys_set()]
    key = request.args.get("shop") or (shops[0]["id"] if shops else "")
    if not shops:
        return jsonify(ok=True, shops=[], shop="")
    try:
        ch = shop(key)
        new, done, sup = lists(ch)
        mp, cat = mpk.mapping(ch), mpk._catalog()
        rows, items = view(ch, new, mp, cat)
        drows, _ = view(ch, done, mp, cat)
    except mpk.ApiError as e:
        return jsonify(ok=False, error=f"{ch.title}: {e.message}"), 502
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, shops=shops, shop=ch.key, kind="wb" if isinstance(ch, mpk.WB) else "ozon", orders=rows, items=items,
                   done=drows, supply={"id": sup["id"], "name": sup.get("name") or "", "count": len(drows)} if sup else None,
                   at=int(time.time()))


# ---------- подтвердить сборку ----------
def ozon_ship(ch, o):
    prods = [{"product_id": int(l["sku"] or l["ref"]), "quantity": l["qty"]} for l in o["lines"]]
    ch.call("/v4/posting/fbs/ship", {"posting_number": o["ext"], "packages": [{"products": prods}], "with": {"additional_data": False}})


def wb_supply_for_today(ch):
    sup = wb_open_supply(ch)
    if sup:
        return sup["id"]
    j = ch.req("POST", "marketplace", "/api/v3/supplies", json={"name": f"{WB_SUPPLY_PREFIX} {datetime.now(mpk.ALMATY):%d.%m}"})
    return j["id"]


def wb_add(ch, supply, oid):
    try:
        ch.req("PATCH", "marketplace", f"/api/v3/supplies/{supply}/orders/{int(oid)}")
    except mpk.ApiError as e:
        if e.code != 404:
            raise
        ch.req("PATCH", "marketplace", f"/api/marketplace/v3/supplies/{supply}/orders", json={"orders": [int(oid)]})   # новый метод WB


def ship(ch, ids):
    """→ {номер: "" (подтверждено) | текст ошибки}."""
    want = [str(i) for i in ids]
    new, _, _ = lists(ch)
    by = {o["ext"]: o for o in new}
    out, supply = {}, None
    for ext in want:
        o = by.get(ext)
        if not o:
            out[ext] = "уже не в новых — обновите список"
            continue
        try:
            if isinstance(ch, mpk.WB):
                supply = supply or wb_supply_for_today(ch)
                wb_add(ch, supply, ext)
            else:
                ozon_ship(ch, o)
            out[ext] = ""
            _err.pop((ch.key, ext), None)
        except mpk.ApiError as e:
            msg = e.message
            if any(w in msg.lower() for w in ("exemplar", "mark", "маркир", "гтд", "rnpt", "kiz", "киз")):
                msg = "нужна маркировка (коды экземпляров) — подтвердите в кабинете: " + msg
            out[ext] = msg[:300]
            _err[(ch.key, ext)] = out[ext]
    print(f"Сборка {ch.title}: подтверждено {sum(1 for v in out.values() if not v)} из {len(out)}"
          + "".join(f"; {k}: {v[:80]}" for k, v in out.items() if v), flush=True)
    return out


def route_ship():
    me, err = mpk._me()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    try:
        ch = shop(b.get("shop"))
        ids = [str(x) for x in b.get("ids") or []][:200]
        if not ids:
            return jsonify(ok=False, error="Отметьте собранные заказы"), 400
        res = ship(ch, ids)
    except mpk.ApiError as e:
        return jsonify(ok=False, error=f"{e.message}"), 502
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, results=res, done=sum(1 for v in res.values() if not v))


# ---------- этикетки одним файлом ----------
def ozon_labels(ch, ids):
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for k in range(0, len(ids), 20):
        part = ids[k:k + 20]
        r = mpk.requests.post(ch.api() + "/v2/posting/fbs/package-label", json={"posting_number": part}, timeout=90,
                              headers={"Client-Id": ch.env("CLIENT_ID"), "Api-Key": ch.env("API_KEY"), "Content-Type": "application/json"})
        if r.status_code >= 400 or not r.content.startswith(b"%PDF"):
            raise mpk.ApiError(r.status_code, str(mpk._json(r).get("message") or f"этикетки не готовы (HTTP {r.status_code})")[:300])
        for pg in PdfReader(io.BytesIO(r.content)).pages:
            w.add_page(pg)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def png_pages(images, w_mm, h_mm):
    """PNG → PDF, по странице на картинку заданного размера (мм)."""
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(w_mm * mm, h_mm * mm))
    for png in images:
        c.drawImage(ImageReader(io.BytesIO(png)), 0, 0, w_mm * mm, h_mm * mm, preserveAspectRatio=True, anchor="c")
        c.showPage()
    c.save()
    return out.getvalue()


def wb_labels(ch, ids):
    got = {}
    for k in range(0, len(ids), 100):
        j = ch.req("POST", "marketplace", "/api/v3/orders/stickers", params={"type": "png", "width": 58, "height": 40},
                   json={"orders": [int(x) for x in ids[k:k + 100]]})
        for s in j.get("stickers") or []:
            if s.get("file"):
                got[str(s.get("orderId"))] = base64.b64decode(s["file"])
    miss = [i for i in ids if i not in got]
    if miss:
        raise mpk.ApiError(404, "нет стикера у заданий " + ", ".join(miss[:10]) + " — они ещё не в поставке")
    return png_pages([got[i] for i in ids], 58, 40)


def route_labels():
    _, err = mpk._me()
    if err:
        return err
    try:
        ch = shop(request.args.get("shop"))
        ids = [x for x in (request.args.get("ids") or "").split(",") if x][:300]
        if not ids:
            return jsonify(ok=False, error="Нет собранных заказов"), 400
        pdf = wb_labels(ch, ids) if isinstance(ch, mpk.WB) else ozon_labels(ch, ids)
    except mpk.ApiError as e:
        return jsonify(ok=False, error=e.message), 502
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    name = f"{'Стикеры WB 58x40' if isinstance(ch, mpk.WB) else 'Этикетки Ozon 75x120'} {datetime.now(mpk.ALMATY):%d.%m %H-%M}.pdf"
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": "inline; filename*=UTF-8''" + _q(name)})


def _q(s):
    from urllib.parse import quote
    return quote(s)


# ---------- WB: закрыть поставку, QR ----------
def wb_qr(ch, supply):
    j = ch.req("GET", "marketplace", f"/api/v3/supplies/{supply}/barcode", params={"type": "png"})
    if not j.get("file"):
        raise mpk.ApiError(404, "WB не отдал QR поставки")
    return png_pages([base64.b64decode(j["file"])], 58, 40)


def route_close():
    me, err = mpk._me()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    try:
        ch = shop(b.get("shop"))
        if not isinstance(ch, mpk.WB):
            return jsonify(ok=False, error="Поставки — только у WB"), 400
        sup = wb_open_supply(ch)
        if not sup or str(sup["id"]) != str(b.get("supply") or ""):
            return jsonify(ok=False, error="Открытая поставка изменилась — обновите страницу"), 409
        ch.req("PATCH", "marketplace", f"/api/v3/supplies/{sup['id']}/deliver")
    except mpk.ApiError as e:
        return jsonify(ok=False, error=e.message), 502
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    print(f"Сборка {ch.title}: поставка {sup['id']} передана в доставку ({me.get('name', '')})", flush=True)
    return jsonify(ok=True, supply=sup["id"])


def route_qr():
    _, err = mpk._me()
    if err:
        return err
    try:
        ch = shop(request.args.get("shop"))
        pdf = wb_qr(ch, str(request.args.get("supply") or ""))
    except mpk.ApiError as e:
        return jsonify(ok=False, error=e.message), 502
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": "inline; filename*=UTF-8''" + _q("QR поставки WB.pdf")})


mpk.bp.add_url_rule("/admin/api/mp/asm", view_func=route_list, endpoint="asm_list")
mpk.bp.add_url_rule("/admin/api/mp/asm/ship", view_func=route_ship, endpoint="asm_ship", methods=["POST"])
mpk.bp.add_url_rule("/admin/api/mp/asm/labels", view_func=route_labels, endpoint="asm_labels")
mpk.bp.add_url_rule("/admin/api/mp/asm/close", view_func=route_close, endpoint="asm_close", methods=["POST"])
mpk.bp.add_url_rule("/admin/api/mp/asm/qr", view_func=route_qr, endpoint="asm_qr")
