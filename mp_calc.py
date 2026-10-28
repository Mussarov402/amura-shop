"""Калькулятор прибыли WB и Ozon (вкладка «Калькулятор» модуля «Каналы продаж», 28.10.2026, задача владельца).

Товар — из МойСклад; по каждому магазину с ключом считаем сверху вниз: цена продажи − комиссия − логистика и доставка −
приёмка и обработка FBS − эквайринг − налог ИП − реклама − закупка = прибыль, маржа от цены; и обратно — минимальная цена
для маржи X %. Ставки только из кабинетов по API, ничего не вшито; нет ключа или товара в кабинете — «нет данных из кабинета».
  Ozon — POST /v5/product/info/prices: price.price (валюта price.currency_code), commissions.sales_percent_fbs,
    fbs_direct_flow_trans_max_amount + fbs_deliv_to_customer_amount (логистика), fbs_first_mile_max_amount (обработка), acquiring.
  WB — цена: GET discounts-prices-api /api/v2/list/goods/filter (sizes[].discountedPrice, currencyIsoCode4217);
    комиссия: GET common-api /api/v1/tariffs/commission (kgvpMarketplace по subjectID карточки, токен с категорией «Тарифы»);
    логистика FBS: GET common-api /api/v1/tariffs/box (boxDeliveryMarketplaceBase + доп. литры по габаритам карточки).
    Тарифа на эквайринг и приёмку в API WB нет — они из отчёта реализации: POST finance-api /api/finance/v1/sales-reports/detailed
    (с 2026 вместо /api/v5/supplier/reportDetailByPeriod; токен с категорией «Финансы», 1 запрос в минуту) — средние за 90 дней по
    товару, иначе по кабинету; продаж нет — «ждём первые продажи». Ozon — факт из /v3/finance/transaction/list (по SKU товара).
    Ставки площадок вручную не вводятся (слово владельца 28.10.2026): только налог ИП, план ДРР, маржа и своя себестоимость.
Реклама — фактический ДРР по товару за 7 и 30 дней = расход на рекламу ÷ сумма заказов × 100; нет расходов — план ДРР (10 %).
  WB: расходы — advert-api /adv/v1/promotion/count (кампании) + GET /adv/v3/fullstats (по дням и nmId, ≤31 дня, ≤50 кампаний,
    3 запроса в минуту; категория «Продвижение»); заказы — statistics-api /api/v1/supplier/orders (категория «Статистика»).
  Ozon: расходы — Performance API (свои ключи OZON_PERF_CLIENT_ID / OZON_PERF_CLIENT_SECRET[_N]): токен /api/client/token,
    кампании /api/client/campaign и их товары /api/client/campaign/{id}/v2/products, расход /api/client/statistics/expense/json;
    расход кампании делится поровну между её товарами. Заказы — Seller API /v1/analytics/data (revenue по sku).
Закупка — buyPrice МойСклад в валюте закупки × курс продажи обменника МиГ (mig.kz, слово владельца 28.10.2026), запасной —
курс Нацбанка РК (с датой), последний — курс валюты в МойСклад;
поле «себестоимость, ₸» владельца важнее рассчитанной. Налог ИП, реклама, эквайринг WB и маржа — настройки модуля.
«Поставить цену» на WB (только владелец, с подтверждением): POST discounts-prices-api /api/v2/upload/task — цена для покупателя
без скидки продавца; статус — /api/v2/buffer/tasks (в обработке) и /api/v2/history/tasks (+ /history/goods/task — ошибки).
Карточки нет — ничего не создаём; валюта кабинета не тенге — не ставим."""
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from flask import jsonify, request

import marketplaces as mpk
import order_hook as oh

NBK_URL = "https://nationalbank.kz/rss/get_rates.cfm?fdate={d}"
MIG_URL = "https://mig.kz/"
SETTINGS = {"tax": "mp_calc_tax", "ads": "mp_calc_ads", "margin": "mp_calc_margin"}   # ставки площадок — только из API
FACT_DAYS, FACT_TTL = 90, 6 * 3600
WAIT = "ждём первые продажи"
WB_FIN_FIELDS = ["nmId", "docTypeName", "quantity", "retailPriceWithDisc", "commissionPercent", "acquiringFee", "deliveryService",
                 "paidAcceptance", "currency", "rrdId", "rrDate"]
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
    if out["ads"] is None:
        out["ads"] = 10.0                                 # план ДРР по умолчанию; факт подставляется, когда есть расходы
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


def today():
    return datetime.now(mpk.ALMATY).strftime("%d.%m.%Y")


def ozon_rates(ch, pid):
    """Тарифы Ozon на сегодня (/v5/product/info/prices) — основа; факт из транзакций за 90 дней — в подписи."""
    ref, mp = _ref_for(ch, pid)
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
    tariff = f"тариф Ozon на {today()}"
    out = {"cur": cur, "price": num(pr.get("price")), "pct": {"commission": num(c.get("sales_percent_fbs"))},
           "amount": {"logistics": num(c.get("fbs_direct_flow_trans_max_amount")) + num(c.get("fbs_deliv_to_customer_amount")),
                      "processing": num(c.get("fbs_first_mile_max_amount")), "acquiring": num(it.get("acquiring"))},
           "notes": {"commission": f"{tariff}, FBS", "logistics": f"{tariff}: магистраль + последняя миля FBS",
                     "processing": f"{tariff}: обработка отправления FBS (максимум)", "acquiring": f"{tariff}: эквайринг"}}
    skus = ((mp["products"].get(ref) or {}).get("skus")) or []
    out["adKeys"] = skus
    fact, where, state = fact_for(ch, skus) if skus else (None, "", "none")
    if fact:
        conv = lambda v: to_kzt(v, cur)[0]
        bits = []
        if fact.get("commission") is not None:
            bits.append(f"комиссия {fact['commission']:.1f} %")
        bits.append(f"логистика {fmt_kzt(conv(fact['logistics']))}")
        bits.append(f"эквайринг {fact['acquiring']:.1f} %")
        out["notes"]["commission"] += f"; факт: {where}: " + ", ".join(bits)
    elif state in ("loading", "error"):
        out["notes"]["commission"] += f"; факт: {where}"
    return out


def fmt_kzt(v):
    return "—" if v is None else f"{round(v):,} ₸".replace(",", " ")


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


def wb_rates(ch, pid):
    """Комиссия и логистика — тарифы WB на сегодня; эквайринг и приёмка (тарифа в API нет) — из отчёта реализации за 90 дней,
    по товару, иначе по кабинету; нет продаж — «ждём первые продажи». Факт по комиссии и логистике — в подписи."""
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
    out = {"cur": cur, "price": price, "pct": {}, "amount": {}, "notes": {}, "missing": {}, "kzt": set(), "adKeys": [card["nm"]]}
    tariff = f"тариф WB на {today()}"
    kg = wb_commissions(ch).get(int(card.get("subject") or 0))
    if kg and kg.get("kgvpMarketplace") is not None:
        out["pct"]["commission"] = float(kg["kgvpMarketplace"])
        out["notes"]["commission"] = f"{tariff}: {kg.get('subjectName', '')}, FBS"
    else:
        out["missing"]["commission"] = "WB не вернул комиссию для категории товара"
    dims = card.get("dims") or {}
    liters = (dims.get("length") or 0) * (dims.get("width") or 0) * (dims.get("height") or 0) / 1000
    box = wb_box(ch)
    if liters:
        cost, note = to_kzt(box["base"] + max(0.0, liters - 1) * box["liter"], box["cur"])
        if cost is None:
            out["missing"]["logistics"] = note
        else:
            out["amount"]["logistics"] = cost
            out["kzt"].add("logistics")
            out["notes"]["logistics"] = f"{tariff}: склад {box['wh']}, {liters:.1f} л" + (f"; {note}" if note else "")
    else:
        out["missing"]["logistics"] = "в карточке WB нет габаритов (Content API)"
    fact, where, state = fact_for(ch, [card["nm"]])
    f = facts(ch) if fact else {}
    if fact:
        out["pct"]["acquiring"] = fact["acquiring"]
        out["notes"]["acquiring"] = where
        acc, note = to_kzt(fact["processing"], f.get("cur"))
        out["amount"]["processing"] = acc
        out["kzt"].add("processing")
        out["notes"]["processing"] = f"{where}: приёмка и обработка" + (f"; {note}" if note else "")
        logi = to_kzt(fact["logistics"], f.get("cur"))[0]
        out["notes"]["logistics"] = out["notes"].get("logistics", out["missing"].get("logistics", "")) + f"; факт: {fmt_kzt(logi)} на продажу"
        if fact.get("commission") is not None:
            out["notes"]["commission"] = out["notes"].get("commission", "") + f"; факт: {fact['commission']:.1f} %"
    else:
        for k in ("acquiring", "processing"):
            out["missing"][k] = where
    return out


# ---------- факт за 90 дней: отчёт реализации WB, транзакции Ozon ----------
_facts_locks = {}                                     # ключ магазина → замок: отчёт грузится один раз, магазины не ждут друг друга


def _bucket():
    return {"sales": 0, "price": 0.0, "kvv": 0.0, "acq": 0.0, "delivery": 0.0, "accept": 0.0, "proc": 0.0}


def _avg(b):
    """Средние на одну продажу: комиссия и эквайринг — % от цены продажи, логистика и приёмка — ₸ (в валюте отчёта)."""
    if not b or b["sales"] <= 0 or b["price"] <= 0:
        return None
    n = b["sales"]
    return {"sales": n, "commission": b["kvv"] / b["price"] * 100 if b["kvv"] else None, "acquiring": b["acq"] / b["price"] * 100,
            "logistics": b["delivery"] / n, "processing": (b["accept"] + b["proc"]) / n}


def wb_fin_rows(ch, d1, d2):
    """Строки детализации отчётов реализации WB за период (частями по rrdId, 1 запрос в минуту)."""
    rid = 0
    for page in range(5):
        if page:
            time.sleep(61)
        rows = ch.req("POST", "finance", "/api/finance/v1/sales-reports/detailed",
                      json={"dateFrom": d1, "dateTo": d2, "limit": 100000, "rrdId": rid, "period": "weekly", "fields": WB_FIN_FIELDS})
        if not isinstance(rows, list) or not rows:
            return
        yield from rows
        rid = rows[-1].get("rrdId") or 0
        if len(rows) < 100000 or not rid:
            return


def wb_facts_load(ch):
    to = datetime.now(mpk.ALMATY)
    d1, d2 = (to - timedelta(days=FACT_DAYS)).strftime("%Y-%m-%d"), to.strftime("%Y-%m-%d")
    by, tot, cur, days, first = {}, _bucket(), "", [], {}
    for x in wb_fin_rows(ch, d1, d2):
        num = lambda k: _wb_num(x.get(k)) or 0.0
        b = by.setdefault(str(x.get("nmId") or ""), _bucket())
        cur = cur or str(x.get("currency") or "")
        if x.get("rrDate"):
            days.append(str(x["rrDate"])[:10])
        q = int(x.get("quantity") or 0)
        sale = str(x.get("docTypeName") or "").lower().startswith("продаж") and q > 0
        if sale and x.get("rrDate"):
            nm = str(x.get("nmId") or "")
            first[nm] = min(first.get(nm, "9999"), str(x["rrDate"])[:10])
        for t in (b, tot):
            if sale:
                t["sales"] += q
                t["price"] += num("retailPriceWithDisc") * q
                t["kvv"] += num("retailPriceWithDisc") * q * num("commissionPercent") / 100
            t["acq"] += num("acquiringFee")
            t["delivery"] += num("deliveryService")
            t["accept"] += num("paidAcceptance")
    span = f"{min(days)[8:10]}.{min(days)[5:7]}–{max(days)[8:10]}.{max(days)[5:7]}" if days else f"{FACT_DAYS} дней"
    return {"by": {k: _avg(v) for k, v in by.items()}, "all": _avg(tot), "cur": cur or "KZT", "span": span,
            "at": int(time.time()), "src": "отчёт реализации WB", "first": first}


OZ_LOGI = ("logistic", "delivtocustomer", "directflow", "lastmile", "delivery")
OZ_PROC = ("dropoff", "firstmile", "pickup", "processing", "fulfillment", "packag")


def ozon_facts_load(ch):
    """Транзакции Ozon за 90 дней (по месяцу за запрос): продажи, комиссия, логистика, обработка, эквайринг по SKU."""
    by, tot, first = {}, _bucket(), {}
    now = datetime.now(timezone.utc)
    days = []
    for k in range(3):
        a, b = now - timedelta(days=30 * (k + 1)), now - timedelta(days=30 * k)
        for page in range(1, 21):
            res = ch.res(ch.call("/v3/finance/transaction/list", {"filter": {"date": {"from": a.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                                                                    "to": b.strftime("%Y-%m-%dT%H:%M:%S.000Z")},
                                                                           "operation_type": [], "posting_number": "", "transaction_type": "all"},
                                                                "page": page, "page_size": 1000}))
            for op in res.get("operations") or []:
                skus = [str(i.get("sku")) for i in op.get("items") or [] if i.get("sku")]
                if not skus:
                    continue
                days.append(str(op.get("operation_date") or "")[:10])
                share = 1 / len(skus)
                acc, typ = float(op.get("accruals_for_sale") or 0), str(op.get("operation_type") or "").lower()
                for sku in skus:
                    if acc > 0 and op.get("operation_date"):
                        first[sku] = min(first.get(sku, "9999"), str(op["operation_date"])[:10])
                    bk = by.setdefault(sku, _bucket())
                    for t in (bk, tot):
                        if acc > 0:
                            t["sales"] += share
                            t["price"] += acc * share
                            t["kvv"] += abs(float(op.get("sale_commission") or 0)) * share
                        if "acquiring" in typ:
                            t["acq"] += abs(float(op.get("amount") or 0)) * share
                        for sv in op.get("services") or []:
                            nm, pr = str(sv.get("name") or "").lower(), abs(float(sv.get("price") or 0)) * share
                            if any(w in nm for w in OZ_LOGI):
                                t["delivery"] += pr
                            elif any(w in nm for w in OZ_PROC):
                                t["proc"] += pr
            if page >= int(res.get("page_count") or 0):
                break
    days = [d for d in days if d]
    span = f"{min(days)[8:10]}.{min(days)[5:7]}–{max(days)[8:10]}.{max(days)[5:7]}" if days else f"{FACT_DAYS} дней"
    return {"by": {k: _avg(v) for k, v in by.items()}, "all": _avg(tot), "cur": "", "span": span, "at": int(time.time()),
            "src": "транзакции Ozon", "first": first}


def facts(ch):
    """Факт по кабинету из кэша (6 ч). Нет в кэше — грузим в фоне (у WB 1 запрос в минуту) и пока отвечаем {"loading": True}.
    Ошибка доступа WB 401/403 — {"error": "у токена WB нет категории «Финансы»"}."""
    key = f"mpc_facts:{ch.key}"
    v = oh._cache.get(key)
    if v and time.time() - v[0] < FACT_TTL:
        return v[1]
    lock = _facts_locks.setdefault(ch.key, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"loading": True}

    def run():
        try:
            data = wb_facts_load(ch) if isinstance(ch, mpk.WB) else ozon_facts_load(ch)
        except mpk.ApiError as e:
            data = {"error": f"у токена {ch.title} нет категории «Финансы»" if isinstance(ch, mpk.WB) and e.code in (401, 403)
                    else f"отчёт {ch.title} не получен: {e.message}"}
        except Exception as e:
            data = {"error": f"отчёт {ch.title} не получен: {str(e)[:200]}"}
        oh._cache[key] = (time.time() if "error" not in data else time.time() - FACT_TTL + 600, data)   # ошибку повторим через 10 мин
        print(f"Факт {ch.key}: " + (data.get("error") or f"{data['src']} {data['span']}, продаж в кабинете "
                                    f"{round((data.get('all') or {}).get('sales') or 0)}"), flush=True)
    def guarded():
        try:
            run()
        finally:
            lock.release()
    if SYNC_FACTS:
        guarded()
        return oh._cache[key][1]
    threading.Thread(target=guarded, daemon=True).start()
    return {"loading": True}


SYNC_FACTS = False                                    # тесты: грузить сразу, без фонового потока


def fact_for(ch, ref_keys):
    """Средние по товару (по любому из ключей: nmID / SKU), иначе по кабинету. → (средние | None, подпись, состояние)."""
    f = facts(ch)
    if f.get("loading"):
        return None, f"загружаю отчёт {ch.title} — обновите через минуту", "loading"
    if f.get("error"):
        return None, f["error"], "error"
    for k in ref_keys:
        a = (f.get("by") or {}).get(str(k))
        if a:
            return a, f"{f['src']} {f['span']}, среднее по товару ({round(a['sales'])} продаж)", "item"
    a = f.get("all")
    if a:
        return a, f"{f['src']} {f['span']}, среднее по кабинету ({round(a['sales'])} продаж)", "all"
    return None, WAIT, "none"


# ---------- реклама: фактический ДРР за 7 и 30 дней ----------
ADS_TTL = 3 * 3600
PERF_URL = "https://api-performance.ozon.ru"
_ads_locks = {}


def _day(dt):
    return dt.strftime("%Y-%m-%d")


def wb_ads_load(ch):
    """{nmId: {"s7", "s30", "r7", "r30"}} — расход на рекламу и сумма заказов за 7 и 30 дней (валюта профиля / заказов)."""
    now = datetime.now(mpk.ALMATY)
    d30, d7 = _day(now - timedelta(days=29)), _day(now - timedelta(days=6))
    by = {}
    row = lambda nm: by.setdefault(str(nm), {"s7": 0.0, "s30": 0.0, "r7": 0.0, "r30": 0.0})
    try:
        cnt = ch.req("GET", "advert", "/adv/v1/promotion/count")
    except mpk.ApiError as e:
        raise mpk.ApiError(e.code, "у токена WB нет категории «Продвижение»" if e.code in (401, 403) else e.message)
    ids = [a["advertId"] for g in cnt.get("adverts") or [] if g.get("status") in (7, 9, 11)
           for a in g.get("advert_list") or [] if a.get("advertId")]
    ads_cur = ""
    for k in range(0, len(ids), 50):
        if k:
            time.sleep(21)                                      # 3 запроса в минуту
        st = ch.req("GET", "advert", "/adv/v3/fullstats", params={"ids": ",".join(map(str, ids[k:k + 50])), "beginDate": d30, "endDate": _day(now)})
        for camp in st if isinstance(st, list) else []:
            ads_cur = ads_cur or str(camp.get("currency") or "")
            for day in camp.get("days") or []:
                dd = str(day.get("date") or "")[:10]
                for app in day.get("apps") or []:
                    for nm in app.get("nms") or []:
                        x, v = row(nm.get("nmId")), float(nm.get("sum") or 0)
                        x["s30"] += v
                        if dd >= d7:
                            x["s7"] += v
    try:
        orders = ch.req("GET", "statistics", "/api/v1/supplier/orders", params={"dateFrom": d30, "flag": 0})
    except mpk.ApiError as e:
        raise mpk.ApiError(e.code, "у токена WB нет категории «Статистика»" if e.code in (401, 403) else e.message)
    for o in orders if isinstance(orders, list) else []:
        dd = str(o.get("date") or "")[:10]
        if o.get("isCancel") or dd < d30:
            continue
        x, v = row(o.get("nmId")), float(o.get("priceWithDisc") or 0)
        x["r30"] += v
        if dd >= d7:
            x["r7"] += v
    return {"by": by, "cur": ads_cur, "span": f"{d30[8:10]}.{d30[5:7]}–{now:%d.%m}", "at": int(time.time())}


def perf_token(ch):
    cid, sec = ch.env("PERF_CLIENT_ID"), ch.env("PERF_CLIENT_SECRET")
    if not (cid and sec):
        return None

    def load():
        r = requests.post(PERF_URL + "/api/client/token", json={"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"},
                          timeout=20)
        if r.status_code >= 400:
            raise mpk.ApiError(r.status_code, f"Ozon Performance не выдал токен (HTTP {r.status_code})")
        return mpk._json(r).get("access_token")
    return oh.cached(f"perf_tok:{ch.key}", 20 * 60, load)


def perf(ch, path, params=None):
    r = requests.get(PERF_URL + path, params=params, timeout=30, headers={"Authorization": f"Bearer {perf_token(ch)}"})
    if r.status_code >= 400:
        raise mpk.ApiError(r.status_code, f"Ozon Performance {path}: HTTP {r.status_code}")
    return mpk._json(r)


def ozon_ads_load(ch):
    now = datetime.now(mpk.ALMATY)
    d30, d7 = _day(now - timedelta(days=29)), _day(now - timedelta(days=6))
    by = {}
    row = lambda sku: by.setdefault(str(sku), {"s7": 0.0, "s30": 0.0, "r7": 0.0, "r30": 0.0})
    if not perf_token(ch):
        raise mpk.ApiError(0, "нет ключей Ozon Performance" + (f" ({ch.env_prefix}_PERF_CLIENT_ID{ch.sfx} и _SECRET на Render)"))
    camps = [c for c in perf(ch, "/api/client/campaign").get("list") or [] if c.get("id")]
    skus = {}
    for c in camps:
        try:
            skus[str(c["id"])] = [str(p.get("sku")) for p in perf(ch, f"/api/client/campaign/{c['id']}/v2/products").get("products") or [] if p.get("sku")]
        except mpk.ApiError:
            skus[str(c["id"])] = []
    if camps:
        rows = perf(ch, "/api/client/statistics/expense/json", {"campaignIds": ",".join(skus), "dateFrom": d30, "dateTo": _day(now)}).get("rows") or []
        for x in rows:
            items = skus.get(str(x.get("id"))) or []
            if not items:
                continue
            v = (_wb_num(x.get("moneySpent")) or 0) + (_wb_num(x.get("bonusSpent")) or 0)
            for sku in items:
                y = row(sku)
                y["s30"] += v / len(items)
                if str(x.get("date") or "")[:10] >= d7:
                    y["s7"] += v / len(items)
    for frm, key in ((d30, "r30"), (d7, "r7")):
        res = ch.res(ch.call("/v1/analytics/data", {"date_from": frm, "date_to": _day(now), "metrics": ["revenue"], "dimension": ["sku"],
                                                    "filters": [], "limit": 1000, "offset": 0}))
        for x in res.get("data") or []:
            dims, mets = x.get("dimensions") or [{}], x.get("metrics") or [0]
            row(dims[0].get("id"))[key] += float(mets[0] or 0)
    return {"by": by, "cur": "", "span": f"{d30[8:10]}.{d30[5:7]}–{now:%d.%m}", "at": int(time.time()),
            "note": "расход кампании делится поровну между её товарами"}


def ads(ch):
    """Реклама магазина из кэша (3 ч); нет — грузим в фоне, пока {"loading": True}. Ошибка — {"error": текст}."""
    key = f"mpc_ads:{ch.key}"
    v = oh._cache.get(key)
    if v and time.time() - v[0] < ADS_TTL:
        return v[1]
    lock = _ads_locks.setdefault(ch.key, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"loading": True}

    def run():
        try:
            try:
                data = wb_ads_load(ch) if isinstance(ch, mpk.WB) else ozon_ads_load(ch)
            except mpk.ApiError as e:
                data = {"error": e.message}
            except Exception as e:
                data = {"error": f"реклама {ch.title} не получена: {str(e)[:200]}"}
            oh._cache[key] = (time.time() if "error" not in data else time.time() - ADS_TTL + 600, data)
            print(f"Реклама {ch.key}: " + (data.get("error") or f"{data['span']}, товаров с данными {len(data['by'])}"), flush=True)
        finally:
            lock.release()
    if SYNC_FACTS:
        run()
        return oh._cache[key][1]
    threading.Thread(target=run, daemon=True).start()
    return {"loading": True}


def drr_for(ch, keys, cur_orders=""):
    """Фактический ДРР товара: → (ДРР 30 дн. | None, подпись). Расходов нет — (None, причина)."""
    a = ads(ch)
    if a.get("loading"):
        return None, "загружаю рекламу — обновите через минуту"
    if a.get("error"):
        return None, a["error"]
    x = next((a["by"].get(str(k)) for k in keys if a["by"].get(str(k))), None)
    if not x or x["s30"] <= 0:
        return None, f"рекламы по товару за {a['span']} нет"
    if x["r30"] <= 0:
        return None, f"расход на рекламу есть, а заказов за {a['span']} нет"
    d7 = f"{x['s7'] / x['r7'] * 100:.1f} %" if x["r7"] > 0 else "—"
    d30 = x["s30"] / x["r30"] * 100
    return d30, f"факт ДРР: 7 дн. {d7}, 30 дн. {d30:.1f} % ({a['span']})" + (f"; {a['note']}" if a.get("note") else "")


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
        raw = ozon_rates(ch, pid) if isinstance(ch, mpk.Ozon) else wb_rates(ch, pid)
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
    for k, v in raw["amount"].items():                  # суммы в валюте цены кабинета; что уже в тенге — в raw["kzt"]
        amount[k] = v if k in raw.get("kzt", ()) else to_kzt(v, raw["cur"])[0]
    pct, cab = dict(raw["pct"]), price_kzt
    if price_override:
        price_kzt = float(price_override)
    if st["tax"] is not None:
        pct["tax"] = st["tax"]
    drr, why = drr_for(ch, raw.get("adKeys") or [])
    pct["ads"] = drr if drr is not None else st["ads"]
    own_notes = {"tax": "настройка владельца",
                 "ads": f"{why}; план {st['ads']:g} %" if drr is not None else f"план ДРР {st['ads']:g} %; {why}"}
    r = calc(price_kzt, pct, amount, prod["cost"], st["margin"])
    notes = {**own_notes, **(raw.get("notes") or {})}
    notes["cost"] = prod["costNote"]
    for k, why in (raw.get("missing") or {}).items():
        notes[k] = why
    if st["tax"] is None:
        notes["tax"] = "задайте налог ИП в настройках расчёта"
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


# ---------- карточка WB по артикулу (только чтение, только владелец; 28.10.2026) ----------
def wb_card(nm):
    """Карточка WB по nmID ключами наших магазинов: Content API (/content/v2/get/cards/list, textSearch) отдаёт только свои
    карточки — нашли, значит наша; цена, скидка и валюта — /api/v2/list/goods/filter. Ничего не меняет.
    Чужая карточка — только «не наша»: данные чужих карточек не берём (публичного API без обхода защиты нет)."""
    shops = [ch for ch in mpk.channels() if isinstance(ch, mpk.WB) and ch.keys_set()]
    if not shops:
        return {"ok": False, "error": f"{NO_DATA}: на Render нет ключа WB"}
    errors = []
    for ch in shops:
        try:
            j = ch.req("POST", "content", "/content/v2/get/cards/list",
                       json={"settings": {"cursor": {"limit": 10}, "filter": {"textSearch": str(nm), "withPhoto": -1}}})
        except mpk.ApiError as e:
            errors.append(f"{ch.title}: {e.message}")
            continue
        c = next((x for x in j.get("cards") or [] if int(x.get("nmID") or 0) == nm), None)
        if not c:
            continue
        price = {}
        try:
            g = ((ch.req("GET", "discounts-prices", "/api/v2/list/goods/filter", params={"limit": 1, "filterNmID": nm})
                  .get("data") or {}).get("listGoods") or [{}])[0]
            sz = (g.get("sizes") or [{}])[0]
            price = {"price": sz.get("price"), "discounted": sz.get("discountedPrice"), "discount": g.get("discount"),
                     "currency": g.get("currencyIsoCode4217") or ""}
        except mpk.ApiError as e:
            price = {"error": f"цена не получена: {e.message}"}
        val = lambda v: ", ".join(map(str, v)) if isinstance(v, list) else str(v)
        dims = c.get("dimensions") or {}
        return {"ok": True, "ours": True, "shop": ch.title, "nm": nm, "title": c.get("title", ""), "subject": c.get("subjectName", ""),
                "brand": c.get("brand", ""), "vendorCode": c.get("vendorCode", ""),
                "barcodes": [b for s in c.get("sizes") or [] for b in s.get("skus") or []],
                "chars": [{"name": x.get("name", ""), "value": val(x.get("value"))} for x in c.get("characteristics") or []],
                "description": c.get("description", ""), "photos": len(c.get("photos") or []), "video": 1 if c.get("video") else 0,
                "dims": {k: dims.get(k) for k in ("length", "width", "height", "weightBrutto")}, "price": price,
                "updated": c.get("updatedAt", "")}
    if errors and len(errors) == len(shops):
        return {"ok": False, "error": f"{NO_DATA}: " + "; ".join(errors)}
    return {"ok": True, "ours": False, "nm": nm}


@mpk.bp.get("/admin/api/mp/wbcard")
def wbcard_route():
    _, err = mpk._me(owner_only=True)
    if err:
        return err
    raw = re.sub(r"\D", "", request.args.get("nm", ""))
    if not raw:
        return jsonify(ok=False, error="Введите артикул WB — только цифры"), 400
    r = wb_card(int(raw))
    return (jsonify(**r), 200) if r["ok"] else (jsonify(**r), 502)
