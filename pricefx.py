"""Цены МойСклад в валюте (баг 17.11.2026: тип цены «Короб» = 5,40 $ читался как 5 ₸ — можно было заказать короб по 5 ₸).

Цена типа МС хранится в своей валюте (salePrices[].currency). Всё, что считает сайт и заказ, — в тенге по курсу МС
(Валюты → rate / multiplicity) на момент расчёта; исходная цена в валюте сохраняется в fx для показа «$5,40 ≈ 2 554 ₸»
и для строки в заказе «Короб $5,40 × курс 472,9». Используется сервером (order_hook) и выгрузкой каталога (export_catalog)."""

KZT = ("KZT", "398")
SIGN = {"USD": "$", "EUR": "€", "RUB": "₽", "CNY": "¥"}
TIERS = ("opt", "mid", "box")
LABEL = {"opt": "Опт", "mid": "От 10 шт", "box": "Короб"}


def cur_map(rows):
    """Строки /entity/currency → id → (ISO, курс к тенге за 1 единицу)."""
    out = {}
    for r in rows or []:
        iso = str(r.get("isoCode") or r.get("code") or "").upper()
        mult = float(r.get("multiplicity") or 1) or 1
        rate = float(r.get("rate") or 1)
        if r.get("indirect"):
            rate = 1 / rate if rate else 0
        out[r["id"]] = (iso, rate / mult)
    return out


def _cid(p):
    return (((p.get("currency") or {}).get("meta") or {}).get("href") or "").rstrip("/").rsplit("/", 1)[-1]


def sale(row, names, cmap):
    """Цена типа (имя или кортеж имён) → (₸ целым, fx или None). fx = {"iso", "v", "rate"} — цена не в тенге.
    Валюта неизвестна (нет в справочнике) — цены нет (0): лучше без цены, чем по ошибке в 100–500 раз дешевле."""
    names = (names,) if isinstance(names, str) else names
    for p in row.get("salePrices") or []:
        if (p.get("priceType") or {}).get("name") not in names:
            continue
        v = (p.get("value") or 0) / 100
        cid = _cid(p)
        if not cid:
            return round(v), None                      # МС не указал валюту — валюта учёта (тенге)
        iso, rate = cmap.get(cid, ("?", 0))
        if iso in KZT:
            return round(v), None
        if not rate or v <= 0:
            return 0, None
        return round(v * rate), {"iso": iso, "v": round(v, 2), "rate": rate}
    return 0, None


def money(fx):
    """{"iso": "USD", "v": 5.4} → «$5,40»."""
    s = f"{fx['v']:.2f}".replace(".", ",")
    sg = SIGN.get(fx["iso"])
    return f"{sg}{s}" if sg else f"{s} {fx['iso']}"


def rate_txt(r):
    t = f"{r:.2f}".rstrip("0").rstrip(".")
    return t.replace(".", ",")


def tiers(row, cmap, names, bq):
    """Опт / от 10 / короб в тенге по правилам сайта (от 10 и короб — только если дешевле опта) + fx по тирам в валюте.
    names = (опт, от 10, короб)."""
    (opt, fo), (mid, fm), (box, fb) = (sale(row, n, cmap) for n in names)
    mid_ok, box_ok = 0 < mid < opt, 0 < box < opt and bq
    fx = {k: f for k, f, ok in (("opt", fo, True), ("mid", fm, mid_ok), ("box", fb, box_ok)) if f and ok}
    return opt, mid if mid_ok else 0, box if box_ok else 0, bq if 0 < box < opt else 0, fx


def reprice(item, cmap_iso):
    """Пересчитать тиры товара с fx по текущему курсу МС (iso → курс). Тир дороже опта после пересчёта — убираем."""
    fx = item.get("fx") or {}
    if not fx:
        return item
    for k in TIERS:
        f = fx.get(k)
        if f and cmap_iso.get(f["iso"]):
            f["rate"] = cmap_iso[f["iso"]]
            item[k] = round(f["v"] * f["rate"])
    opt = item.get("opt") or 0
    for k in ("mid", "box"):
        if item.get(k) and not 0 < item[k] < opt:
            item[k] = 0
            fx.pop(k, None)
    if not item.get("box"):
        fx.pop("box", None)
    item["fx"] = fx
    return item


def guard(item):
    """Страховка: тир меньше 100 ₸ при опте от 100 ₸ — почти наверняка цена в валюте, прочитанная как тенге
    (старая выгрузка каталога без fx). Такой тир не продаём."""
    opt = item.get("opt") or 0
    bad = []
    for k in ("mid", "box"):
        if item.get(k) and opt >= 100 and item[k] < 100 and k not in (item.get("fx") or {}):
            item[k] = 0
            bad.append(k)
    return bad


def note(name, tier, fx, kzt):
    """Строка в заказ: «Короб $5,40 × курс 472,9 = 2 554 ₸ — <товар>»."""
    return f"{LABEL.get(tier, tier)} {money(fx)} × курс {rate_txt(fx['rate'])} = {kzt:,} ₸ — {name}".replace(f"{kzt:,}", f"{kzt:,}".replace(",", " "))
