"""Склад: приёмка, списание, оприходование и перемещение из панели с двойной записью в МойСклад (этап 3 дорожной карты, docs/MIGRATION.md).

Как устроено:
- операция сначала сохраняется у нас (таблица wh_op, статус queued) — она не теряется, даже если МойСклад не ответил;
- сразу же отправляется в МойСклад (приёмка supply / списание loss), externalCode документа = id операции;
- при сбое сети или 5xx операция остаётся в очереди и повторяется в фоне; перед повтором документ ищется
  в МойСклад по externalCode — если запрос всё-таки прошёл, второй документ не создаётся;
- ответ 4xx (МойСклад отверг данные) не повторяется сам: статус error, владелец видит причину и нажимает «Повторить».
МойСклад остаётся главным: остатки меняет его документ, наше зеркало подтянет его при следующем проходе.
Флаг feat_wh_ops: по решению владельца 10.10.2026 включён, пока его не выключили вручную; работает только при включённом модуле «Склад»."""
import json
import os
import threading
import time
import uuid

import inbox
import mirror
import modules
import order_hook as oh

FLAG = "feat_wh_ops"
KINDS = {"supply": "Приёмка", "loss": "Списание", "enter": "Оприходование", "move": "Перемещение", "salesreturn": "Возврат покупателя",
         "purchasereturn": "Возврат поставщику", "purchaseorder": "Заказ поставщику"}
RETURNS = {"salesreturn": "demand", "purchasereturn": "supply"}   # возврат → документ, по которому он оформляется (колонка demand_id)
AGENT_KINDS = ("supply", "salesreturn", "purchasereturn", "purchaseorder")
BASED = {**RETURNS, "supply": "purchaseorder"}   # документ-основание в колонке demand_id: у приёмки — заказ поставщику (необязательно)
CUR_KINDS = ("supply", "purchaseorder", "purchasereturn", "salesreturn")   # документы, которые бывают в валюте (доллар и т.п.)
PRICED = ("supply", "enter", "purchaseorder")   # цену вводит человек (закупка / себестоимость); у списания и перемещения — закупочная из карточки
RETRY_EVERY = 60          # с, фоновый повтор очереди
MAX_AUTO = 30             # после стольких неудачных попыток — только вручную
_ready = [False]
_lock = threading.Lock()  # одна отправка за раз: повтор из фона и нажатие «Повторить» не пересекаются


def _schema(d):
    if _ready[0]:
        return
    d.run("CREATE TABLE IF NOT EXISTS wh_op (id TEXT PRIMARY KEY, kind TEXT, store_id TEXT, agent_id TEXT, descr TEXT,"
          " lines TEXT, status TEXT, ms_id TEXT, ms_number TEXT, attempts INTEGER DEFAULT 0, error TEXT, who TEXT,"
          " created DOUBLE PRECISION, sent DOUBLE PRECISION, next_try DOUBLE PRECISION DEFAULT 0)")
    d.c.commit()
    # добавочные колонки: склад-получатель у перемещения, отгрузка у возврата; валюта документа (пусто — тенге) и курс
    for col, typ in (("store2_id", "TEXT"), ("demand_id", "TEXT"), ("cur", "TEXT"), ("rate", "DOUBLE PRECISION")):
        try:
            d.run(f"ALTER TABLE wh_op ADD COLUMN {col} {typ}")
            d.c.commit()
        except Exception:
            d.c.rollback()
    _ready[0] = True


def db():
    d = inbox.db()
    _schema(d)
    return d


def enabled():
    if not modules.enabled("warehouse"):
        return False
    with inbox.db() as d:
        return inbox.get_setting(d, FLAG, "1") == "1"     # решение владельца 10.10.2026: включено сразу, выключатель — в «Товары → Склад»


def set_enabled(on):
    with inbox.db() as d:
        inbox.set_setting(d, FLAG, "1" if on else "0")


def _row(r):
    i, kind, store, agent, descr, lines, status, ms_id, ms_num, att, err, who, created, sent, store2, demand, cur, rate = r[:18]
    return {"id": i, "kind": kind, "title": KINDS.get(kind, kind), "store": store, "store2": store2 or "", "agent": agent, "descr": descr or "",
            "lines": json.loads(lines or "[]"), "status": status, "msId": ms_id or "", "msNumber": ms_num or "",
            "attempts": att or 0, "error": err or "", "who": who or "", "created": created or 0, "sent": sent or 0, "demand": demand or "",
            "cur": cur or "", "rate": rate or 1}


_OCOLS = "id, kind, store_id, agent_id, descr, lines, status, ms_id, ms_number, attempts, error, who, created, sent, store2_id, demand_id, cur, rate"


def get(op_id):
    with db() as d:
        r = d.run(f"SELECT {_OCOLS} FROM wh_op WHERE id=%s", (op_id,), one=True)
    return _row(r) if r else None


def recent(limit=30):
    with db() as d:
        rows = d.run(f"SELECT {_OCOLS} FROM wh_op ORDER BY created DESC LIMIT %s", (limit,), many=True) or []
        names = dict(d.run("SELECT id, name FROM ms_store", many=True) or [])
        agents = {}
        ids = list({r[3] for r in rows if r[3]})
        if ids:
            agents = dict(d.run("SELECT id, name FROM ms_agent WHERE id IN (" + ", ".join(["%s"] * len(ids)) + ")", ids, many=True) or [])
    out = []
    for r in rows:
        o = _row(r)
        o["storeName"], o["agentName"], o["store2Name"] = names.get(o["store"], ""), agents.get(o["agent"], ""), names.get(o["store2"], "")
        sc = sum(x["qty"] * x["price"] for x in o["lines"])          # в валюте документа
        o["sum"] = round(sc * (o["rate"] if o["cur"] else 1))         # в тенге
        o["orig"] = mirror.money_label(o["cur"], sc, o["rate"])
        out.append(o)
    po = [o["msId"] for o in out if o["kind"] == "purchaseorder" and o["msId"]]
    if po:                                     # заказ поставщику: уже принят по нему товар?
        with db() as d:
            got = {r[0]: (r[1], r[2]) for r in d.run("SELECT demand_id, status, ms_number FROM wh_op WHERE kind='supply' AND status<>'error' AND demand_id IN ("
                                                     + ", ".join(["%s"] * len(po)) + ")", po, many=True) or []}
        for o in out:
            if o["kind"] == "purchaseorder" and o["msId"] in got:
                o["received"] = {"status": got[o["msId"]][0], "msNumber": got[o["msId"]][1] or ""}
    return out


def demand_lines(demand_id, kind="salesreturn"):
    """Документ для возврата (отгрузка — возврат покупателя, приёмка — возврат поставщику): контрагент, склад и позиции —
    было в документе, уже возвращено (из системы и прямо в МойСклад), можно вернуть; цена со скидкой."""
    base = RETURNS[kind]
    with mirror.db() as d:
        x = d.run("SELECT id, number, agent_id, store_id, applicable, cur, rate FROM ms_doc WHERE id=%s AND type=%s AND deleted=0",
                  (demand_id, base), one=True)
        if not x:
            return None
        rows = d.run("SELECT l.product_id, p.name, p.code, p.kind, SUM(l.qty), SUM(l.sum) FROM ms_doc_line l LEFT JOIN ms_product p ON p.id=l.product_id"
                     " WHERE l.doc_id=%s GROUP BY l.product_id, p.name, p.code, p.kind ORDER BY p.name", (demand_id,), many=True) or []
    back = {}
    with db() as d:
        for (lines,) in d.run("SELECT lines FROM wh_op WHERE kind=%s AND demand_id=%s AND status<>'error'", (kind, demand_id), many=True) or []:
            for l in json.loads(lines or "[]"):
                back[l["id"]] = back.get(l["id"], 0) + l["qty"]
        mine = {r[0] for r in d.run("SELECT ms_id FROM wh_op WHERE kind=%s AND demand_id=%s AND ms_id<>''", (kind, demand_id), many=True) or []}
    with mirror.db() as d:                     # возвраты, сделанные прямо в МойСклад (зеркало знает их основание), — кроме наших
        for doc_id, pid, q in d.run("SELECT x.id, l.product_id, l.qty FROM ms_doc x JOIN ms_doc_line l ON l.doc_id=x.id"
                                    " WHERE x.type=%s AND x.base_id=%s AND x.deleted=0 AND x.applicable=1", (kind, demand_id), many=True) or []:
            if doc_id not in mine:
                back[pid] = back.get(pid, 0) + (q or 0)
    cur, rate = x[5] or "", x[6] or 1
    # цена — в валюте документа (позиции в зеркале — в тенге по курсу документа): возврат по долларовой приёмке — тоже в долларах
    price = (lambda sm, q: round(sm / q / rate, 2)) if cur else (lambda sm, q: round(sm / q))
    out = [{"id": pid, "name": n or "(товар удалён)", "code": c or "", "kind": k or "product", "qty": q or 0, "returned": back.get(pid, 0),
            "left": round((q or 0) - back.get(pid, 0), 3), "price": price(sm or 0, q) if q else 0} for pid, n, c, k, q, sm in rows]
    return {"id": x[0], "number": x[1] or "", "agent": x[2] or "", "store": x[3] or "", "applicable": bool(x[4]), "lines": out,
            "cur": cur, "rate": rate}


CUR_DOCS = ("supply", "purchasereturn")   # у каких документов МойСклад владелец может сменить валюту из системы


def set_currency(kind, doc_id, cur, rate=None, who=""):
    """Сменить валюту документа в МойСклад (владелец, явное действие): цифры цен остаются, меняется валюта и курс —
    так исправляют приёмку, в которую долларовые цены внесли в тенговый документ. cur — USD/…; пусто или KZT — тенге.
    PUT идемпотентен: повтор не создаёт ничего нового. Возвращает {cur, rate, number}."""
    if kind not in CUR_DOCS:
        raise ValueError("Валюту можно сменить только у приёмки или возврата поставщику")
    cur = "" if (cur or "").upper() in ("", "KZT") else cur.upper()
    mirror._load_cur()
    if cur:
        try:
            rate = float(rate) if rate not in (None, "") else mirror.cur_rate(cur)
        except (TypeError, ValueError):
            raise ValueError("Курс — число")
        if not rate or rate <= 0 or rate > 100000:
            raise ValueError("Укажите курс")
        cid = mirror.cur_id(cur)
        if not cid:
            raise ValueError(f"В МойСклад нет валюты {cur}")
        body = {"rate": {"currency": oh.meta("currency", cid), "value": rate}}
    else:
        if not mirror._CUR["default"]:
            raise ValueError("Не загружены валюты МойСклад — попробуйте через минуту")
        rate, body = 1.0, {"rate": {"currency": oh.meta("currency", mirror._CUR["default"])}}
    row = oh.ms("PUT", f"/entity/{kind}/{doc_id}", json=body, timeout=30)
    s = (row.get("sum") or 0) / 100
    with mirror.db() as d:                     # наша копия сразу видит новую валюту; позиции (в тенге) — пересчёт по новому курсу
        old = (d.run("SELECT rate FROM ms_doc WHERE id=%s", (doc_id,), one=True) or [None])[0] or 1
        k = rate / old
        d.run("UPDATE ms_doc SET cur=%s, rate=%s, sum_cur=%s, sum=%s, paid=COALESCE(paid, 0)*%s WHERE id=%s", (cur, rate, s, s * rate, k, doc_id))
        d.run("UPDATE ms_doc_line SET price=price*%s, sum=sum*%s WHERE doc_id=%s", (k, k, doc_id))
    print(f"Склад: у документа {kind} №{row.get('name')} валюта сменена на {cur or 'KZT'} (курс {rate:g}){' — ' + who if who else ''}", flush=True)
    return {"cur": cur, "rate": rate, "number": row.get("name") or "", "sum": round(s * rate), "orig": mirror.money_label(cur, s, rate)}


def create_return(demand_id, lines, descr="", who="", kind="salesreturn"):
    """Возврат по документу: покупателя — по отгрузке, поставщику — по приёмке. Склад и контрагент — из документа,
    цены — как в нём, не больше, чем было в документе (за вычетом прежних возвратов — из системы и из МойСклад)."""
    if kind not in RETURNS:
        raise ValueError("Неизвестный возврат")
    what = "Отгрузка" if kind == "salesreturn" else "Приёмка"
    dm = demand_lines(demand_id, kind)
    if not dm or not dm["applicable"]:
        raise ValueError(f"{what} не найдена или не проведена")
    if not dm["agent"] or not dm["store"]:
        raise ValueError(f"У документа нет контрагента или склада")
    have = {l["id"]: l for l in dm["lines"]}
    clean = []
    for x in lines or []:
        pid = str(x.get("id") or "")
        try:
            q = float(x.get("qty") or 0)
        except (TypeError, ValueError):
            raise ValueError("Количество — число")
        if q <= 0:
            continue
        if pid not in have:
            raise ValueError("Этого товара нет в документе")
        if q > have[pid]["left"] + 1e-9:
            raise ValueError(f"«{have[pid]['name'][:40]}»: вернуть можно не больше {have[pid]['left']:g} шт.")
        clean.append({"id": pid, "qty": q, "price": have[pid]["price"]})
    if not clean:
        raise ValueError("Укажите, сколько возвращают")
    return create(kind, dm["store"], clean, dm["agent"], descr, who, demand_id=demand_id, cur=dm["cur"], rate=dm["rate"])


def create(kind, store_id, lines, agent_id="", descr="", who="", store2_id="", op_id="", demand_id="", cur="", rate=None):
    """Проверяет и сохраняет операцию, затем сразу пробует отправить в МойСклад. Ошибка проверки — ValueError.
    op_id — свой id операции (инвентаризация): если такая операция уже есть, вторая не создаётся.
    cur — валюта документа (USD; пусто или KZT — тенге): цены в этой валюте, в МойСклад документ уходит с rate (курс — текущий
    курс валюты МойСклад, у возврата — курс документа-основания)."""
    if op_id and get(op_id):
        return send(op_id)
    if kind not in KINDS:
        raise ValueError("Неизвестная операция")
    if kind in RETURNS and not demand_id:
        raise ValueError("Возврат оформляется из документа (отгрузки или приёмки)")
    cur = "" if (cur or "").upper() in ("", "KZT") else cur.upper()
    if kind == "supply" and demand_id:              # приёмка по заказу поставщику, созданному в системе
        with db() as d:
            po = d.run("SELECT agent_id, cur FROM wh_op WHERE kind='purchaseorder' AND status='sent' AND ms_id=%s", (demand_id,), one=True)
        if not po:
            raise ValueError("Заказ поставщику не найден")
        if po[0] != agent_id:
            raise ValueError("Поставщик приёмки не совпадает с заказом")
        if (po[1] or "") != cur:
            raise ValueError("Валюта приёмки не совпадает с заказом")
    if cur:
        if kind not in CUR_KINDS:
            raise ValueError("Этот документ оформляется только в тенге")
        rate = rate or mirror.cur_rate(cur)
        if not rate or not mirror.cur_id(cur):
            raise ValueError(f"В МойСклад нет валюты {cur} или её курса")
    rate = rate if cur else 1
    descr = (descr or "").strip()[:500]
    with db() as d:
        if not d.run("SELECT 1 FROM ms_store WHERE id=%s AND archived=0", (store_id,), one=True):
            raise ValueError("Выберите склад")
        if kind in AGENT_KINDS and not d.run("SELECT 1 FROM ms_agent WHERE id=%s AND deleted=0", (agent_id or "",), one=True):
            raise ValueError("Выберите поставщика")
        if kind == "move":
            if not store2_id or store2_id == store_id or not d.run("SELECT 1 FROM ms_store WHERE id=%s AND archived=0", (store2_id,), one=True):
                raise ValueError("Выберите склад, куда перемещаете (другой)")
        else:
            store2_id = ""
        clean = []
        for x in lines or []:
            pid = str(x.get("id") or "")
            try:
                qty, price = float(x.get("qty") or 0), float(x.get("price") or 0)
            except (TypeError, ValueError):
                raise ValueError("Количество и цена — числа")
            if qty <= 0 or price < 0:
                raise ValueError("Количество должно быть больше нуля")
            p = d.run("SELECT kind, name, buy_price FROM ms_product WHERE id=%s AND deleted=0", (pid,), one=True)
            if not p:
                raise ValueError("Товар не найден — обновите список")
            pr = price if kind in PRICED + tuple(RETURNS) else (p[2] or 0)
            clean.append({"id": pid, "kind": p[0] or "product", "name": p[1], "qty": qty,
                          "price": round(pr, 2) if cur else round(pr)})          # в валюте — с центами, в тенге — без тиынов
        if not clean:
            raise ValueError("Добавьте хотя бы один товар")
        op_id = op_id or str(uuid.uuid4())
        d.run(f"INSERT INTO wh_op ({_OCOLS}, next_try) VALUES (%s, %s, %s, %s, %s, %s, 'queued', '', '', 0, '', %s, %s, 0, %s, %s, %s, %s, 0)",
              (op_id, kind, store_id, agent_id if kind in AGENT_KINDS else "", descr, json.dumps(clean, ensure_ascii=False), who, time.time(),
               store2_id, demand_id if kind in BASED else "", cur, rate))
    send(op_id)
    return get(op_id)


def _body(o):
    pos = [{"quantity": x["qty"], "price": round(x["price"] * 100), "assortment": oh.meta(x.get("kind") or "product", x["id"])}
           for x in o["lines"]]
    body = {"externalCode": o["id"], "organization": oh.meta("organization", oh.organization()),
            "positions": pos, "applicable": True,
            "description": ("Из панели AMURA" + (f" ({o['who']})" if o["who"] else "") + (": " + o["descr"] if o["descr"] else ""))[:4000]}
    if o["kind"] == "move":            # перемещение: склад-отправитель и склад-получатель
        body["sourceStore"], body["targetStore"] = oh.meta("store", o["store"]), oh.meta("store", o["store2"])
    else:
        body["store"] = oh.meta("store", o["store"])
    if o["kind"] in AGENT_KINDS:
        body["agent"] = oh.meta("counterparty", o["agent"])
    if o["kind"] in RETURNS:                    # ссылка на документ: demand у возврата покупателя, supply у возврата поставщику
        body[RETURNS[o["kind"]]] = oh.meta(RETURNS[o["kind"]], o["demand"])
    if o["kind"] == "supply" and o["demand"]:   # приёмка по заказу поставщику
        body["purchaseOrder"] = oh.meta("purchaseorder", o["demand"])
    if o["cur"]:                                 # валютный документ: цены в валюте, курс — явно (иначе МойСклад примет доллары за тенге)
        cid = mirror.cur_id(o["cur"])
        if not cid:
            raise ValueError(f"В МойСклад нет валюты {o['cur']}")
        body["rate"] = {"currency": oh.meta("currency", cid), "value": o["rate"]}
    return body


def _mark(op_id, **f):
    sets = ", ".join(f"{k}=%s" for k in f)
    with db() as d:
        d.run(f"UPDATE wh_op SET {sets} WHERE id=%s", (*f.values(), op_id))


def send(op_id):
    """Одна попытка отправить операцию. Возвращает её состояние. Никогда не создаёт второй документ в МойСклад."""
    with _lock:
        o = get(op_id)
        if not o or o["status"] == "sent":
            return o
        att = o["attempts"] + 1
        _mark(op_id, attempts=att)       # до запроса: если процесс упадёт посреди записи, повтор сначала поищет документ
        try:
            if o["attempts"]:      # прошлая попытка могла дойти: сначала ищем документ по externalCode
                found = oh.ms("GET", f"/entity/{o['kind']}", params={"filter": f"externalCode={o['id']}", "limit": 1}, timeout=20).get("rows") or []
                if found:
                    _mark(op_id, status="sent", ms_id=found[0]["id"], ms_number=found[0].get("name") or "", attempts=att, error="", sent=time.time())
                    print(f"Склад: операция «{KINDS[o['kind']]}» {op_id[:8]} уже есть в МойСклад (№{found[0].get('name')})", flush=True)
                    return get(op_id)
            r = oh.ms("POST", f"/entity/{o['kind']}", json=_body(o), timeout=30)
            _mark(op_id, status="sent", ms_id=r.get("id") or "", ms_number=r.get("name") or "", attempts=att, error="", sent=time.time())
            print(f"Склад: операция «{KINDS[o['kind']]}» {op_id[:8]} проведена в МойСклад №{r.get('name')}", flush=True)
        except Exception as e:
            msg = str(e)[:300]
            rejected = msg.startswith("МойСклад 4")      # данные отвергнуты — сам не повторяем
            _mark(op_id, status="error" if rejected else "queued", attempts=att, error=msg,
                  next_try=time.time() + min(3600, RETRY_EVERY * 2 ** min(att, 6)))
            print(f"Склад: операция «{KINDS[o['kind']]}» {op_id[:8]} не отправлена (попытка {att}): {msg}", flush=True)
        return get(op_id)


def retry(op_id):
    """«Повторить» из панели: в том числе после отказа МойСклад (например, поправили карточку товара)."""
    o = get(op_id)
    if o and o["status"] == "error":
        _mark(op_id, status="queued")
    return send(op_id)


def retry_pending():
    with db() as d:
        now = time.time()           # created < now−60: только что созданную операцию отправляет сам create()
        ids = [r[0] for r in d.run("SELECT id FROM wh_op WHERE status='queued' AND attempts<%s AND next_try<=%s AND created<%s ORDER BY created",
                                   (MAX_AUTO, now, now - 60), many=True) or []]
    for i in ids:
        send(i)
    return len(ids)


def _loop():
    time.sleep(120)
    while True:
        try:
            if enabled():
                retry_pending()
        except Exception as e:
            print("Склад (очередь):", e, flush=True)
        time.sleep(RETRY_EVERY)


def agents(q, limit=20):
    """Поставщики для приёмки — из зеркала контрагентов."""
    q = (q or "").strip()
    with mirror.db() as d:
        rows = d.run("SELECT id, name, phone FROM ms_agent WHERE deleted=0 AND archived=0 AND (LOWER(name) LIKE %s OR name LIKE %s OR phone LIKE %s)"
                     " ORDER BY name LIMIT %s", (f"%{q.lower()}%", f"%{q}%", f"%{q}%", limit), many=True) or []
    import audit
    with mirror.db() as d:                         # валюта, в которой поставщик обычно работает (для предупреждения в форме)
        return [{"id": i, "name": n, "phone": p or "", "cur": audit.usual_cur(d, i)} for i, n, p in rows]


if os.environ.get("PORT"):          # только на сервере; пока флаг выключен, очередь не трогается
    threading.Thread(target=_loop, daemon=True).start()
