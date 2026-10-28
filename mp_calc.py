"""Калькулятор прибыли WB и Ozon (вкладка «Калькулятор» модуля «Каналы продаж», 28.10.2026, задача владельца).

Товар — из МойСклад; по каждому магазину с ключом считаем сверху вниз: цена продажи − комиссия − логистика и доставка −
приёмка и обработка FBS − эквайринг − налог ИП − реклама − закупка = прибыль, маржа от цены; и обратно — минимальная цена
для маржи X %. Ставки только из кабинетов по API, ничего не вшито; нет ключа или товара в кабинете — «нет данных из кабинета».
  Ozon — POST /v5/product/info/prices: price.price (валюта price.currency_code), commissions.sales_percent_fbs,
    fbs_direct_flow_trans_max_amount + fbs_deliv_to_customer_amount (логистика), fbs_first_mile_max_amount (обработка), acquiring.
  WB — цена: GET discounts-prices-api /api/v2/list/goods/filter (sizes[].discountedPrice, currencyIsoCode4217);
    комиссия: GET common-api /api/v1/tariffs/commission (kgvpMarketplace по subjectID карточки, токен с категорией «Тарифы»);
    логистика FBS: GET common-api /api/v1/tariffs/box (boxDeliveryMarketplaceBase + доп. литры по габаритам карточки).
    Приёмки и обработки FBS и ставки эквайринга в API WB нет: эквайринг WB — настройка владельца, приёмка — «нет в API».
Закупка — buyPrice МойСклад в валюте закупки × курс продажи обменника МиГ (mig.kz, слово владельца 28.10.2026), запасной —
курс Нацбанка РК (с датой), последний — курс валюты в МойСклад;
поле «себестоимость, ₸» владельца важнее рассчитанной. Налог ИП, реклама, эквайринг WB и маржа — настройки модуля.
«Поставить цену» на WB (только владелец, с подтверждением): POST discounts-prices-api /api/v2/upload/task — цена для покупателя
без скидки продавца; статус — /api/v2/buffer/tasks (в обработке) и /api/v2/history/tasks (+ /history/goods/task — ошибки).
Карточки нет — ничего не создаём; валюта кабинета не тенге — не ставим."""
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests
from flask import jsonify, request

import marketplaces as mpk
import order_hook as oh

NBK_URL = "https://nationalbank.kz/rss/get_rates.cfm?fdate={d}"
MIG_URL = "https://mig.kz/"
SETTINGS = {"tax": "mp_calc_tax", "ads": "mp_calc_ads", "wbacq": "mp_calc_wbacq", "margin": "mp_calc_margin"}
NO_DATA = "нет данных из кабинета"


# ---------- курсы Нацбанка РК ----------
def nbk_rates():
    """Курсы Нацбанка РК на сегодня: {"date": "ДД.ММ.ГГГГ", "rates": {"USD": тенге за 1, ...}}; кэш 3 часа."""
    def load():
        d = datetime.now(mpk.ALMATY).strftime("%d.%m.%Y")
        r = requests.get(NBK_URL.format(d=d), timeout=15, headers={"User-Agent": "amura-admin/1.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        rates = {}
        for it in root.iter("item"):
            code, val, q = (it.findtext("title") or "").strip().upper(), it.findtext("description"), it.findtext("quant")
            try:
                rates[code] = float(str(val).replace(",", ".")) / max(1.0, float(q or 1))
            except (TypeError, ValueError):
                continue
        if not rates:
            raise ValueError("Нацбанк вернул пустой список курсов")
        return {"date": (root.findtext("date") or d).strip(), "rates": rates}
    return oh.cached("nbk_rates", 3 * 3600, load)


def parse_mig(html):
    """Курсы продажи МиГ со страницы mig.kz: строки таблицы <td class="buy …">покупка</td><td class="currency">USD</td>
    <td class="sell …">продажа</td>. {"USD": тенге за 1, ...}."""
    out = {}
    for code, sell in re.findall(r'<td[^>]*class="currency"[^>]*>\s*([A-Z]{3})\s*</td>\s*<td[^>]*class="sell[^"]*"[^>]*>\s*([\d.,]+)', html):
        try:
            out[code] = float(sell.replace(",", "."))
        except ValueError:
            continue
    return out


def mig_rates():
    """Курс продажи обменника МиГ (mig.kz) — по нему владелец покупает валюту; кэш 30 минут, время — когда взяли (Алматы)."""
    def load():
        r = requests.get(MIG_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0 (amura-admin)"})
        r.raise_for_status()
        rates = parse_mig(r.text)
        if not rates:
            raise ValueError("на mig.kz не нашёл таблицу курсов")
        return {"date": datetime.now(mpk.ALMATY).strftime("%d.%m.%Y %H:%M"), "rates": rates}
    return oh.cached("mig_rates", 1800, load)


def to_kzt(amount, cur, ms_rate=None):
    """Сумма в тенге и откуда курс: (тенге, «курс продажи МиГ на время» / «курс НБ РК на дату» / «курс МойСклад»).
    Не перевели — (None, причина)."""
    cur = (cur or "KZT").upper()
    if cur in ("KZT", "398", ""):
        return float(amount), ""
    if cur in ("RUR", "643"):
        cur = "RUB"
    try:
        mg = mig_rates()
        if cur in mg["rates"]:
            r = mg["rates"][cur]
            return float(amount) * r, f"курс продажи МиГ {mg['date']}: 1 {cur} = {r:g} ₸"
    except Exception as e:
        print("Курс МиГ не получен:", str(e)[:200], flush=True)
    try:
        nb = nbk_rates()
        if cur in nb["rates"]:
            r = nb["rates"][cur]
            return float(amount) * r, f"курс НБ РК {nb['date']}: 1 {cur} = {r:g} ₸ (МиГ не ответил)"
    except Exception as e:
        print("Курс Нацбанка не получен:", str(e)[:200], flush=True)
    if ms_rate:
        return float(amount) * ms_rate, f"курс МойСклад: 1 {cur} = {ms_rate:g} ₸ (МиГ и Нацбанк не ответили)"
    return None, f"нет курса {cur}"


# ---------- настройки ----------
def settings():
    out = {}
    for k, key in SETTINGS.items():
        v = mpk.setting(key, "")
        try:
            out[k] = float(v) if v != "" else None
        except ValueError:
            out[k] = None
    if out["margin"] is None:
        out["margin"] = 20.0
    return out


def cost_override(pid):
    v = mpk.setting(f"mp_cost_{pid}", "")
    try:
        return float(v) if v != "" else None
    except ValueError:
        return None


# ---------- товар МойСклад ----------
def product(pid):
    """Товар МойСклад: название, код и закупка в тенге (своя себестоимость владельца — важнее)."""
    def load():
        return oh.ms("GET", f"/entity/product/{pid}", params={"expand": "buyPrice.currency"}, timeout=15)
    p = oh.cached(f"mpc_prod:{pid}", 600, load)
    bp = p.get("buyPrice") or {}
    cur = bp.get("currency") or {}
    raw, iso = (bp.get("value") or 0) / 100, (cur.get("isoCode") or "KZT")
    own = cost_override(pid)
    if own is not None:
        cost, note = own, "себестоимость задана вручную"
    elif raw:
        cost, note = to_kzt(raw, iso, cur.get("rate"))
        note = f"закупка МойСклад {raw:g} {iso}" + (f", {note}" if note else "")
    else:
        cost, note = None, "в МойСклад нет закупочной цены"
    bars = [str(v) for b in p.get("barcodes") or [] for v in b.values() if v]
    return {"id": pid, "name": p.get("name", ""), "code": p.get("code", ""), "buy": raw, "buyCur": iso, "barcodes": bars,
            "cost": None if cost is None else round(cost), "costNote": note, "own": own}


def search(q):
    q = (q or "").strip().lower()
    if len(q) < 2:
        return []
    rows = [i for i in mpk._catalog().values()
            if q in " ".join(str(i.get(k) or "") for k in ("name", "code", "article", "barcode")).lower()]
    rows.sort(key=lambda i: (str(i.get("code") or "").lower() != q, i.get("name", "")))
    return [{"id": i["id"], "name": i.get("name", ""), "code": i.get("code", "")} for i in rows[:20]]


# ---------- ставки из кабинетов ----------
def _ref_for(ch, pid):
    mp = mpk.mapping(ch)
    return next((ref for ref, mid in mp["ref"].items() if mid == pid), None), mp


def ozon_rates(ch, pid):
    ref, _ = _ref_for(ch, pid)
    if not ref:
        return {"error": "товара нет в кабинете Ozon (нет пары по штрихкоду или артикулу)"}
    r = ch.call("/v5/product/info/prices", {"filter": {"product_id": [ref], "visibility": "ALL"}, "cursor": "", "limit": 1})
    items = r.get("items") or ch.res(r).get("items") or []
    if not items:
        return {"error": NO_DATA}
    it = items[0]
    c, pr = it.get("commissions") or {}, it.get("price") or {}
    cur = str(pr.get("currency_code") or "")
    num = lambda v: float(v or 0)
    return {"cur": cur, "price": num(pr.get("price")), "pct": {"commission": num(c.get("sales_percent_fbs"))},
            "amount": {"logistics": num(c.get("fbs_direct_flow_trans_max_amount")) + num(c.get("fbs_deliv_to_customer_amount")),
                       "processing": num(c.get("fbs_first_mile_max_amount")), "acquiring": num(it.get("acquiring"))},
            "notes": {"logistics": "магистраль + последняя миля FBS", "processing": "обработка отправления FBS (максимум)",
                      "acquiring": "эквайринг Ozon"}}


def _wb_num(v):
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def wb_box(ch):
    """Тарифы WB для коробов на сегодня; склад — казахстанский, иначе первый с тарифом FBS."""
    def load():
        j = ch.req("GET", "common", "/api/v1/tariffs/box", params={"date": datetime.now(mpk.ALMATY).strftime("%Y-%m-%d")})
        data = ((j.get("response") or {}).get("data") or {})
        rows = [w for w in data.get("warehouseList") or [] if _wb_num(w.get("boxDeliveryMarketplaceBase")) is not None]
        if not rows:
            raise ValueError("WB не вернул тарифы логистики FBS")
        kz = [w for w in rows if re.search(r"казахстан|алмат|астан", f"{w.get('geoName', '')} {w.get('warehouseName', '')}", re.I)]
        w = (kz or rows)[0]
        return {"cur": data.get("currency") or "RUB", "base": _wb_num(w.get("boxDeliveryMarketplaceBase")),
                "liter": _wb_num(w.get("boxDeliveryMarketplaceLiter")) or 0, "wh": w.get("warehouseName", "")}
    return oh.cached(f"mpc_box:{ch.key}", 3600, load)


def wb_commissions(ch):
    def load():
        rep = ch.req("GET", "common", "/api/v1/tariffs/commission", params={"locale": "ru"}).get("report") or []
        return {int(x["subjectID"]): x for x in rep if x.get("subjectID")}
    return oh.cached(f"mpc_kgvp:{ch.key}", 3600, load)


def wb_rates(ch, pid, acq_pct):
    ref, mp = _ref_for(ch, pid)
    card = (mp["products"].get(ref) or {}) if ref else {}
    if not ref or not card.get("nm"):
        return {"error": "товара нет в кабинете WB (нет пары по штрихкоду или артикулу)"}
    goods = (ch.req("GET", "discounts-prices", "/api/v2/list/goods/filter", params={"limit": 1, "filterNmID": card["nm"]})
             .get("data") or {}).get("listGoods") or []
    if not goods:
        return {"error": NO_DATA}
    g = goods[0]
    size = next((s for s in g.get("sizes") or [] if str(s.get("sizeID") or s.get("chrtID") or "") == str(ref)), (g.get("sizes") or [{}])[0])
    price = float(size.get("discountedPrice") or size.get("price") or 0)
    cur = str(g.get("currencyIsoCode4217") or "")
    out = {"cur": cur, "price": price, "pct": {}, "amount": {}, "notes": {}, "missing": {}}
    kg = wb_commissions(ch).get(int(card.get("subject") or 0))
    if kg and kg.get("kgvpMarketplace") is not None:
        out["pct"]["commission"] = float(kg["kgvpMarketplace"])
        out["notes"]["commission"] = f"{kg.get('subjectName', '')}, FBS"
    else:
        out["missing"]["commission"] = "нет комиссии для категории товара"
    dims = card.get("dims") or {}
    liters = (dims.get("length") or 0) * (dims.get("width") or 0) * (dims.get("height") or 0) / 1000
    box = wb_box(ch)
    if liters:
        cost, note = to_kzt(box["base"] + max(0.0, liters - 1) * box["liter"], box["cur"])
        if cost is None:
            out["missing"]["logistics"] = note
        else:
            out["amount"]["logistics"] = cost
            out["notes"]["logistics"] = f"склад {box['wh']}, {liters:.1f} л; {note}".strip("; ")
            out["kzt_amounts"] = True
    else:
        out["missing"]["logistics"] = "в карточке WB нет габаритов"
    out["missing"]["processing"] = "в API WB нет"
    if acq_pct is None:
        out["missing"]["acquiring"] = "задайте эквайринг WB, %"
    else:
        out["pct"]["acquiring"] = acq_pct
        out["notes"]["acquiring"] = "настройка владельца"
    return out


# ---------- расчёт ----------
LINES = [("commission", "Комиссия"), ("logistics", "Логистика и доставка"), ("processing", "Приёмка и обработка FBS"),
         ("acquiring", "Эквайринг"), ("tax", "Налог ИП"), ("ads", "Реклама"), ("cost", "Закупка")]


def calc(price, pct, amount, cost, margin):
    """Чистый расчёт в тенге. pct — доли от цены (%), amount — суммы; None — нет данных (в прибыль не идёт, но отмечается).
    Минимальная цена для маржи: P = (суммы + закупка) / (1 − проценты − маржа)."""
    lines, known_pct, fixed = [], 0.0, 0.0
    for key, label in LINES:
        if key == "cost":
            v = cost
        elif key in pct and pct[key] is not None:
            v = price * pct[key] / 100
            known_pct += pct[key]
        else:
            v = amount.get(key)
            if v is not None:
                fixed += v
        lines.append({"key": key, "label": label, "sum": None if v is None else round(v),
                      "pct": pct.get(key)})
    if cost is not None:
        fixed += cost
    total = sum(x["sum"] for x in lines if x["sum"] is not None)
    profit = round(price - total) if price else None
    den = 1 - (known_pct + margin) / 100
    return {"price": round(price), "lines": lines, "profit": profit,
            "margin": round(profit / price * 100, 1) if price and profit is not None else None,
            "minPrice": round(fixed / den) if den > 0 else None,
            "complete": all(x["sum"] is not None for x in lines)}


def log_line(c):
    if c.get("error"):
        return c["error"]
    part = lambda x: f"{x['label'].lower()} " + ("нет" if x["sum"] is None else (f"{x['pct']:g}% = " if x.get("pct") is not None else "") + f"{x['sum']} ₸")
    return f"цена {c['price']} ₸, " + ", ".join(part(x) for x in c["lines"] if x["key"] in ("commission", "logistics", "processing", "acquiring"))


def channel_calc(ch, pid, prod, st, price_override=None):
    try:
        raw = ozon_rates(ch, pid) if isinstance(ch, mpk.Ozon) else wb_rates(ch, pid, st["wbacq"])
    except mpk.ApiError as e:
        return {"id": ch.key, "title": ch.title, "error": f"{NO_DATA}: {e.message}"}
    except Exception as e:
        return {"id": ch.key, "title": ch.title, "error": f"{NO_DATA}: {str(e)[:200]}"}
    if raw.get("error"):
        return {"id": ch.key, "title": ch.title, "error": raw["error"]}
    price_kzt, rate_note = to_kzt(raw["price"], raw["cur"])
    if price_kzt is None:
        return {"id": ch.key, "title": ch.title, "error": f"цена в {raw['cur']}: {rate_note}"}
    amount = {}
    for k, v in raw["amount"].items():                  # у Ozon суммы в валюте цены, у WB логистика уже в тенге
        amount[k] = v if k == "logistics" and raw.get("kzt_amounts") else to_kzt(v, raw["cur"])[0]
    pct, cab = dict(raw["pct"]), price_kzt
    if price_override:
        price_kzt = float(price_override)
    for k in ("tax", "ads"):
        if st[k] is not None:
            pct[k] = st[k]
    r = calc(price_kzt, pct, amount, prod["cost"], st["margin"])
    notes = dict(raw.get("notes") or {})
    notes["cost"] = prod["costNote"]
    for k, why in (raw.get("missing") or {}).items():
        notes[k] = why
    for k, label in (("tax", "задайте налог ИП, %"), ("ads", "задайте рекламу, %")):
        if st[k] is None:
            notes[k] = label
    for x in r["lines"]:
        x["note"] = notes.get(x["key"], "")
    return {"id": ch.key, "title": ch.title, "cabinetPrice": round(cab),
            "priceNote": rate_note, **r}


# ---------- цена на WB (кнопка владельца) ----------
TASK_STATUS = {1: "в обработке", 3: "цена обновилась", 4: "загрузка отменена", 5: "обработана с ошибками", 6: "не обновилась — ошибки"}


def wb_nm(ch, pid, bars):
    """Артикул WB (nmID) товара МойСклад: по паре канала, иначе поиск карточки по штрихкоду в Content API. Нет — None."""
    ref, mp = _ref_for(ch, pid)
    nm = ((mp["products"].get(ref) or {}).get("nm")) if ref else None
    if nm:
        return int(nm)
    for b in bars:
        j = ch.req("POST", "content", "/content/v2/get/cards/list",
                   json={"settings": {"cursor": {"limit": 10}, "filter": {"textSearch": b, "withPhoto": -1}}})
        for c in j.get("cards") or []:
            if any(b in (sz.get("skus") or []) for sz in c.get("sizes") or []):
                return int(c["nmID"])
    return None


def wb_set_price(ch, pid, price):
    """Цена для покупателя на WB без скидки продавца. → {"ok", "task"} или {"ok": False, "error"}; ничего не создаём."""
    prod = product(pid)
    nm = wb_nm(ch, pid, prod.get("barcodes") or [])
    if not nm:
        return {"ok": False, "error": f"На {ch.title} нет карточки этого товара (искал по штрихкоду {', '.join(prod.get('barcodes') or []) or '—'}) — ничего не создаю"}
    goods = (ch.req("GET", "discounts-prices", "/api/v2/list/goods/filter", params={"limit": 1, "filterNmID": nm})
             .get("data") or {}).get("listGoods") or []
    cur = str((goods[0] if goods else {}).get("currencyIsoCode4217") or "")
    if cur.upper() not in ("KZT", "398"):
        return {"ok": False, "error": f"Валюта кабинета {ch.title} — {cur or 'не указана'}, а не тенге: цену не ставлю"}
    j = ch.req("POST", "discounts-prices", "/api/v2/upload/task", json={"data": [{"nmID": nm, "price": int(price), "discount": 0}]})
    if j.get("error"):
        return {"ok": False, "error": f"WB: {j.get('errorText') or 'ошибка загрузки'}"}
    tid = (j.get("data") or {}).get("id")
    task = {"id": tid, "nm": nm, "price": int(price), "at": int(time.time()), "status": "в обработке"}
    mpk.set_setting(f"mp_price_{ch.key}_{pid}", json.dumps(task, ensure_ascii=False))
    print(f"Цена WB: {ch.key} nm {nm} → {int(price)} ₸, загрузка {tid}", flush=True)
    return {"ok": True, "task": task}


def wb_task(ch, pid, refresh=True):
    """Последняя загрузка цены по товару и её статус у WB (buffer — ещё в обработке, history — обработана)."""
    try:
        task = json.loads(mpk.setting(f"mp_price_{ch.key}_{pid}", "") or "null")
    except ValueError:
        task = None
    if not task or not refresh or not task.get("id") or task.get("final"):
        return task
    try:
        b = (ch.req("GET", "discounts-prices", "/api/v2/buffer/tasks", params={"uploadID": task["id"]}).get("data") or {})
        if b.get("status") == 1:
            task["status"] = TASK_STATUS[1]
        else:
            h = ch.req("GET", "discounts-prices", "/api/v2/history/tasks", params={"uploadID": task["id"]}).get("data") or {}
            st = h.get("status")
            task["status"] = TASK_STATUS.get(st, f"статус {st}")
            task["final"] = st in (3, 4, 5, 6)
            if st in (5, 6):
                g = (ch.req("GET", "discounts-prices", "/api/v2/history/goods/task", params={"uploadID": task["id"], "limit": 10})
                     .get("data") or {}).get("historyGoods") or []
                task["errors"] = [str(x.get("errorText") or "") for x in g if x.get("errorText")][:5]
        mpk.set_setting(f"mp_price_{ch.key}_{pid}", json.dumps(task, ensure_ascii=False))
    except mpk.ApiError as e:
        task["status"] = f"не узнал статус: {e.message}"
    return task


# ---------- маршруты ----------
@mpk.bp.get("/admin/api/mp/calc")
def calc_route():
    _, err = mpk._me()
    if err:
        return err
    pid = request.args.get("id", "")
    st = settings()
    if not pid:
        return jsonify(ok=True, settings=st, items=search(request.args.get("q", "")))
    try:
        prod = product(pid)
    except Exception as e:
        return jsonify(ok=False, error=f"Товар не найден в МойСклад: {str(e)[:200]}"), 404
    prices = {}
    try:
        prices = {k: float(v) for k, v in json.loads(request.args.get("prices") or "{}").items() if float(v) > 0}
    except (ValueError, TypeError, AttributeError):
        pass
    chans = [channel_calc(ch, pid, prod, st, prices.get(ch.key)) if ch.keys_set()
             else {"id": ch.key, "title": ch.title, "error": f"{NO_DATA}: на Render не вписаны {ch.env_names()}"}
             for ch in mpk.channels()]
    for c, ch in zip(chans, mpk.channels()):
        if isinstance(ch, mpk.WB) and ch.keys_set():
            c["wb"] = True
            c["priceTask"] = wb_task(ch, pid, refresh=False)
    print(f"Калькулятор {prod['code']}: закупка {prod['cost']} ₸ ({prod['costNote']}); "      # для проверки по логам, без ключей
          + "; ".join(f"{c['id']}: {log_line(c)}" for c in chans), flush=True)
    return jsonify(ok=True, settings=st, product=prod, channels=chans, at=int(time.time()))


@mpk.bp.post("/admin/api/mp/calc/settings")
def calc_settings_route():
    _, err = mpk._me(owner_only=True)
    if err:
        return err
    d = request.get_json(silent=True) or {}
    for k, key in SETTINGS.items():
        if k in d:
            v = d[k]
            if v in (None, ""):
                mpk.set_setting(key, "")
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                return jsonify(ok=False, error="Проценты — числом"), 400
            if not 0 <= v < 100:
                return jsonify(ok=False, error="Проценты — от 0 до 99"), 400
            mpk.set_setting(key, f"{v:g}")
    if d.get("id"):
        v = d.get("cost")
        if v in (None, ""):
            mpk.set_setting(f"mp_cost_{d['id']}", "")
        else:
            try:
                mpk.set_setting(f"mp_cost_{d['id']}", f"{max(0.0, float(v)):g}")
            except (TypeError, ValueError):
                return jsonify(ok=False, error="Себестоимость — числом, в тенге"), 400
        oh._cache.pop(f"mpc_prod:{d['id']}", None)
    return jsonify(ok=True, settings=settings())


@mpk.bp.route("/admin/api/mp/calc/price", methods=["GET", "POST"])
def price_route():
    """POST {id, shop, price} — поставить цену на WB (только владелец, кнопка с подтверждением); GET ?id=&shop= — статус загрузки."""
    _, err = mpk._me(owner_only=True)
    if err:
        return err
    d = (request.get_json(silent=True) or {}) if request.method == "POST" else request.args
    mpk.channels()
    ch = mpk.CHANNELS.get(str(d.get("shop") or ""))
    if not isinstance(ch, mpk.WB) or not ch.keys_set():
        return jsonify(ok=False, error="Нет такого магазина WB с ключом"), 400
    pid = str(d.get("id") or "")
    if not pid:
        return jsonify(ok=False, error="Не выбран товар"), 400
    try:
        if request.method == "GET":
            return jsonify(ok=True, task=wb_task(ch, pid))
        try:
            price = int(round(float(d.get("price"))))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="Цена — числом, в тенге"), 400
        if price < 1:
            return jsonify(ok=False, error="Цена должна быть больше нуля"), 400
        r = wb_set_price(ch, pid, price)
    except mpk.ApiError as e:
        return jsonify(ok=False, error=f"WB: {e.message}"), 502
    return (jsonify(**r), 200) if r["ok"] else (jsonify(**r), 409)
