"""Выгрузка каталога AMURA из МойСклад для сайта.

Запускается в GitHub Actions репо Agents (там есть MS_TOKEN).
Пишет в папку OUT (по умолчанию ./docs — папка сайта в этом же репо):
  catalog.json        — товары в наличии с ценами Оптовая (1–9 шт) / От 15шт (на сайте от 10 шт) / Короб
  img/<id>.webp       — миниатюры 500px (перекачиваются только если товар изменился)
  img/index.json      — кэш: id -> updated
ИИ не используется. Токенов в выходных файлах нет.
"""
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image

API = "https://api.moysklad.ru/api/remap/1.2"
TOKEN = os.environ["MS_TOKEN"]
OUT = os.environ.get("OUT", "docs")
IMG_DIR = os.path.join(OUT, "img")
IMG_SIZE = 500
NEW_DAYS = 21

PRICE_OPT = "Оптовая цена"
PRICE_MID = ("От 10шт", "От 15шт")   # тип цены переименован в МойСклад; на сайте действует от 10 шт
PRICE_BOX = "Короб"
SKIP_CODES = {"00308"}  # «Услуга грузчика» и прочие служебные позиции

S = requests.Session()
S.headers.update({"Authorization": f"Bearer {TOKEN}", "Accept-Encoding": "gzip"})


def get(url, **params):
    for attempt in range(5):
        r = S.get(url, params=params, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 + attempt * 3)
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()


def fetch_assortment():
    rows, offset = [], 0
    while True:
        data = get(f"{API}/entity/assortment",
                   filter="type=product;stockMode=positiveOnly;archived=false",
                   limit=500, offset=offset).json()
        rows += data.get("rows", [])
        offset += 500
        if offset >= data["meta"]["size"]:
            return rows


def price(item, name):
    names = (name,) if isinstance(name, str) else name
    for p in item.get("salePrices", []):
        if p.get("priceType", {}).get("name") in names:
            return round(p.get("value", 0) / 100)
    return 0


def brand_of(item):
    for a in item.get("attributes", []) or []:
        if a.get("name") == "Бренд":
            v = a.get("value")
            return v.get("name") if isinstance(v, dict) else str(v)
    return (item.get("pathName") or "").split("/")[0].strip()


def box_qty(item):
    packs = item.get("packs") or []
    qtys = [int(p.get("quantity", 0)) for p in packs if p.get("quantity", 0) > 1]
    return max(qtys) if qtys else 0


def countries():
    rows = get(f"{API}/entity/country", limit=1000).json().get("rows", [])
    return {r["id"]: r["name"] for r in rows}


def country_of(item, cmap):
    href = (item.get("country") or {}).get("meta", {}).get("href", "")
    return cmap.get(href.rsplit("/", 1)[-1], "") if href else ""


def barcode_of(item):
    for b in item.get("barcodes") or []:
        for v in b.values():
            if isinstance(v, str) and v.isdigit():
                return v
    return ""


def fetch_image(pid):
    rows = get(f"{API}/entity/product/{pid}/images", limit=1).json().get("rows", [])
    if not rows:
        return None
    href = rows[0]["meta"]["downloadHref"]
    r = S.get(href, timeout=60, allow_redirects=True)
    r.raise_for_status()
    im = Image.open(io.BytesIO(r.content)).convert("RGB")
    im.thumbnail((IMG_SIZE, IMG_SIZE))
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=80, method=6)
    return buf.getvalue()


def main():
    os.makedirs(IMG_DIR, exist_ok=True)
    idx_path = os.path.join(IMG_DIR, "index.json")
    try:
        img_index = json.load(open(idx_path, encoding="utf-8"))
    except Exception:
        img_index = {}

    rows = fetch_assortment()
    cmap = countries()
    now = datetime.now(timezone(timedelta(hours=5)))
    new_since = (now - timedelta(days=NEW_DAYS)).strftime("%Y-%m-%d")
    items, downloaded, no_opt = [], 0, 0

    for it in rows:
        if it.get("code") in SKIP_CODES:
            continue
        qty = int(it.get("quantity") if it.get("quantity") is not None else it.get("stock") or 0)  # доступно = остаток − резерв
        opt = price(it, PRICE_OPT)
        if qty <= 0:
            continue
        if opt <= 0:
            no_opt += 1
            continue
        mid, box, bq = price(it, PRICE_MID), price(it, PRICE_BOX), box_qty(it)
        pid, upd = it["id"], it.get("updated", "")

        img = None
        if it.get("images", {}).get("meta", {}).get("size", 0) > 0:
            path = os.path.join(IMG_DIR, f"{pid}.webp")
            if img_index.get(pid) == upd and os.path.exists(path):
                img = f"img/{pid}.webp"
            else:
                try:
                    data = fetch_image(pid)
                    if data:
                        open(path, "wb").write(data)
                        img_index[pid] = upd
                        img = f"img/{pid}.webp"
                        downloaded += 1
                except Exception as e:
                    print("img fail", pid, e, file=sys.stderr)

        items.append({
            "id": pid,
            "name": it.get("name", ""),
            "brand": brand_of(it),
            "code": it.get("code", ""),
            "article": it.get("article", ""),
            "desc": (it.get("description") or "")[:4000],
            "country": country_of(it, cmap),
            "barcode": barcode_of(it),
            "qty": qty,
            "opt": opt,
            "mid": mid if 0 < mid < opt else 0,
            "box": box if (0 < box < opt and bq) else 0,
            "boxQty": bq if (0 < box < opt) else 0,
            "img": img,
            "updated": upd[:10],
            "isNew": upd[:10] >= new_since,
        })

    # удалить миниатюры товаров, которых больше нет
    alive = {i["id"] for i in items}
    for pid in list(img_index):
        if pid not in alive:
            img_index.pop(pid, None)
            try:
                os.remove(os.path.join(IMG_DIR, f"{pid}.webp"))
            except FileNotFoundError:
                pass

    catalog = {"updated": now.strftime("%d.%m.%Y %H:%M"), "items": items}
    json.dump(catalog, open(os.path.join(OUT, "catalog.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    json.dump(img_index, open(idx_path, "w", encoding="utf-8"))
    print(f"товаров: {len(items)}, новых фото: {downloaded}, без оптовой цены пропущено: {no_opt}, "
          f"с ценой короба: {sum(1 for i in items if i['box'])}")


if __name__ == "__main__":
    main()
