"""Инбокс AMURA: диалоги клиентов из Telegram-бота, ответы ИИ (OpenAI) и менеджера.
Хранение — Postgres (DATABASE_URL); без неё временный SQLite-файл (при перезапуске сервера пропадёт).
ИИ: OPENAI_API_KEY, модель OPENAI_MODEL (по умолчанию gpt-4o-mini)."""
import base64
import json
import os
import re
import sqlite3
import threading
import time

import requests

import order_hook as oh

DB_URL = os.environ.get("DATABASE_URL", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
HANDOFF = "[[MANAGER]]"
PG = bool(DB_URL)
_lock = threading.Lock()
_ready = [False]

DEFAULT_RULES = """Ты — менеджер по продажам оптового интернет-магазина корейской косметики AMURA (склад в Алматы, доставка по Казахстану).
Отвечай коротко, дружелюбно, на языке клиента (русский или казахский), без выдумок: цены и наличие бери только из блока «Товары из каталога», остальное — из базы знаний.
Если не знаешь ответа, клиент просит скидку сверх правил, жалуется, спорит об оплате, хочет изменить или отменить заказ, или просит живого менеджера — коротко скажи, что передаёшь менеджеру, и добавь в самом конце ответа метку """ + HANDOFF + """.
Заказ можно оформить прямо в этом чате (см. раздел «Оформление заказа») или клиент оформит его сам на сайте."""


# ---------- база ----------
class DB:
    def __init__(self):
        if PG:
            import psycopg2
            self.c = psycopg2.connect(DB_URL, connect_timeout=10)
        else:
            self.c = sqlite3.connect(os.environ.get("INBOX_SQLITE", "/tmp/amura_inbox.db"), check_same_thread=False)
        self.cur = self.c.cursor()

    def run(self, sql, args=(), one=False, many=False, ins=False):
        if not PG:
            sql = sql.replace("%s", "?")
        if ins and PG:
            sql += " RETURNING id"
        self.cur.execute(sql, args)
        if ins:
            return self.cur.fetchone()[0] if PG else self.cur.lastrowid
        if one:
            return self.cur.fetchone()
        if many:
            return self.cur.fetchall()

    def __enter__(self):
        return self

    def __exit__(self, et, *a):
        (self.c.rollback if et else self.c.commit)()
        self.c.close()


def db():
    d = DB()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        for s in (
            f"CREATE TABLE IF NOT EXISTS conv (id {pk}, chat_id TEXT UNIQUE, name TEXT, username TEXT, status TEXT DEFAULT 'ai', unread INTEGER DEFAULT 0, last_at DOUBLE PRECISION, last_text TEXT)",
            f"CREATE TABLE IF NOT EXISTS msg (id {pk}, conv_id INTEGER, role TEXT, text TEXT, photo TEXT, at DOUBLE PRECISION)",
            f"CREATE TABLE IF NOT EXISTS kb (id {pk}, title TEXT, body TEXT, at DOUBLE PRECISION)",
            "CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT)",
        ):
            d.run(s)
        d.c.commit()
        _ready[0] = True
    return d


def get_setting(d, key, default=""):
    r = d.run("SELECT value FROM setting WHERE key=%s", (key,), one=True)
    return r[0] if r else default


def set_setting(d, key, value):
    if d.run("SELECT 1 FROM setting WHERE key=%s", (key,), one=True):
        d.run("UPDATE setting SET value=%s WHERE key=%s", (value, key))
    else:
        d.run("INSERT INTO setting (key, value) VALUES (%s, %s)", (key, value))


def ai_on(d):
    return bool(OPENAI_KEY) and get_setting(d, "ai_enabled", "1") == "1"


# ---------- оформление заказа ИИ ----------
ORDER_RULES = """# Оформление заказа
Ты можешь оформить заказ в МойСклад инструментом create_order. Порядок строго такой:
1. Собери у клиента: какие товары и сколько (бери id из блока «Товары из каталога»), имя, телефон, город, способ отправки (kamaz КАМАЗ, rail ЖД, avia Авиа — для них нужна логистическая компания; kazpost Казпочта — нужны ФИО получателя, индекс из 6 цифр и адрес; courier курьер по Алматы — нужен адрес; pickup самовывоз).
2. Покажи клиенту сводку: товары, количество, цену за штуку, итог с учётом комиссии банка 0,95% (и услуги грузчика 1 000 ₸ для КАМАЗ/ЖД/Авиа), способ отправки, имя, телефон, город. Строку с итогом начни словом «Итого».
3. Только после явного согласия клиента («да», «подтверждаю», «оформляй») вызови create_order с client_confirmed=true. Никогда не оформляй заказ без подтверждения сводки.
4. Если заказ не удалось создать или сумма очень большая — не пытайся ещё раз, передай менеджеру (метка """ + HANDOFF + """).
После успешного заказа накладная и реквизиты оплаты уходят клиенту автоматически — просто коротко подтверди номер заказа."""

MEDIA_RULES = """# Фото и голосовые
Клиент может прислать фото или голосовое (оно уже расшифровано и помечено 🎤). Относись к ним как к обычному тексту.
- Фото товара или упаковки: прочитай бренд и название на упаковке, найди товар инструментом search_catalog и назови цену и наличие. Если уверенно определить не получилось — честно скажи и попроси название или артикул.
- Фото чека об оплате или скриншот перевода: поблагодари и передай менеджеру на проверку (метка """ + HANDOFF + """), оплату сам не подтверждай.
- Фото брака, повреждения, жалобы: извинись, попроси номер заказа и передай менеджеру (метка """ + HANDOFF + """).
- Фото, на котором нет ничего про заказ или товары, — вежливо уточни, чем помочь.
Никогда не выдумывай то, чего не видно на фото."""

SEARCH_TOOL = {"type": "function", "function": {
    "name": "search_catalog",
    "description": "Найти товары в каталоге по названию, бренду или артикулу (например, по тексту с фото упаковки). Возвращает id, цены и наличие.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}

ORDER_TOOL = [{"type": "function", "function": {
    "name": "create_order",
    "description": "Оформить заказ клиента в МойСклад. Вызывать только после того, как клиент подтвердил сводку заказа.",
    "parameters": {"type": "object", "properties": {
        "items": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string", "description": "id товара из каталога"}, "qty": {"type": "integer"}}, "required": ["id", "qty"]}},
        "name": {"type": "string"}, "phone": {"type": "string"}, "city": {"type": "string"},
        "shipping": {"type": "string", "enum": ["kamaz", "rail", "avia", "kazpost", "courier", "pickup"]},
        "logistics": {"type": "string", "description": "логистическая компания для kamaz/rail/avia"},
        "recipient": {"type": "string"}, "zip": {"type": "string"}, "address": {"type": "string"},
        "client_confirmed": {"type": "boolean", "description": "true только если клиент явно подтвердил сводку"}},
        "required": ["items", "name", "phone", "city", "shipping", "client_confirmed"]}}}, SEARCH_TOOL]

AI_ORDER_MAX = int(os.environ.get("AI_ORDER_MAX", "3000000"))      # заказы дороже ИИ не оформляет — только менеджер
AI_ORDERS_PER_DAY = 3
_ai_orders = {}
_ai_done = {}          # ключ заказа -> (время, ответ модели): повторный вызов не создаёт второй заказ


def run_create_order(args, ctx, history):
    """Выполняет вызов create_order от модели. Возвращает текст-результат для модели."""
    if ctx is None:                                              # пробный чат в панели: ничего не создаём
        return "ТЕСТОВЫЙ РЕЖИМ: заказ в МойСклад не создавался. Скажи клиенту, что заказ оформлен (тест), номер 0000."
    if not args.get("client_confirmed"):
        return "Ошибка: клиент не подтвердил сводку. Покажи сводку и дождись согласия."
    last_ai = next((t for r, t in reversed(history) if r == "ai"), "")
    if "итого" not in last_ai.lower():
        return "Ошибка: сначала покажи клиенту сводку заказа со словом «Итого» и дождись подтверждения."
    chat, now = str(ctx["chat"]), time.time()
    done = [x for x in _ai_orders.get(chat, []) if now - x < 86400]
    if len(done) >= AI_ORDERS_PER_DAY:
        return "Ошибка: лимит заказов через чат на сегодня. Передай диалог менеджеру (метка " + HANDOFF + ")."
    items = [{"id": str(i.get("id")), "qty": int(i.get("qty") or 0)} for i in (args.get("items") or [])][:50]
    d = {"items": items, "name": args.get("name", ""), "phone": args.get("phone", ""), "city": args.get("city", ""),
         "shipping": args.get("shipping"), "logistics": args.get("logistics", ""), "recipient": args.get("recipient", ""),
         "zip": args.get("zip", ""), "address": args.get("address", ""), "telegram": ctx.get("username", "")}
    key = "tg:" + chat + ":" + oh.hashlib.sha1(json.dumps([items, d["shipping"], time.strftime("%Y%m%d")], sort_keys=True).encode()).hexdigest()[:16]
    for k in [k for k, v in _ai_done.items() if now - v[0] > 600]:
        _ai_done.pop(k, None)
    if key in _ai_done:
        return _ai_done[key][1]
    payload, status = oh.order_core(d, key, "tg:" + chat, None, source="из Telegram (ИИ-продажник)")
    if status != 200:
        return "Ошибка: " + str(payload.get("error", "не удалось оформить")) + ". Исправь данные с клиентом или передай менеджеру."
    if payload["total"] > AI_ORDER_MAX:
        oh.alert("aiorder", f"ИИ оформил заказ № {payload['number']} на {oh.fmt(payload['total'])} ₸ — выше лимита, проверьте", every=0)
    _ai_orders.setdefault(chat, []).append(now)
    data = payload["_data"]
    done_msg = f"Заказ № {data['number']} уже создан на сумму {oh.fmt(data['total'])} ₸ и повторно не создаётся. Накладная и реквизиты отправлены клиенту."
    _ai_done[key] = (now, done_msg)
    try:
        pdf = oh.build_pdf(data)
        oh.pdf_store(data["number"], pdf)
        oh.tg("sendDocument", chat_id=chat, caption=f"Ваш заказ AMURA № {data['number']} на {oh.fmt(data['total'])} ₸.",
              _files={"document": (f"AMURA-{data['number']}.pdf", pdf, "application/pdf")})
        oh.tg("sendMessage", chat_id=chat, text=oh.pay_text())
    except Exception as e:
        print("ИИ-заказ: накладная не ушла клиенту:", e, flush=True)
        return f"Заказ № {data['number']} создан на {oh.fmt(data['total'])} ₸, но накладную отправит менеджер. Сообщи клиенту номер заказа."
    return f"Заказ № {data['number']} создан на сумму {oh.fmt(data['total'])} ₸. Накладная и реквизиты оплаты отправлены клиенту в чат."


# ---------- ИИ ----------
def _words(t):
    return [w for w in re.findall(r"[\w-]{3,}", t.lower())][:8]


def product_context(text):
    v = oh._cache.get("live")
    if not v:
        return "Каталог сейчас недоступен."
    ws = oh.PUBLIC_WHOLESALE
    ws_words = _words(text)
    scored = []
    for i in v[1]["items"]:
        hay = (i["name"] + " " + (i.get("brand") or "")).lower()
        sc = sum(w in hay for w in ws_words)
        if sc:
            scored.append((sc, i))
    scored.sort(key=lambda x: -x[0])
    rows = []
    for _, i in scored[:6]:
        p = f"розница {oh.fmt(i.get('rtl', 0))} ₸"
        if ws:
            p = f"опт {oh.fmt(i.get('opt', 0))} ₸" + (f", от 10 шт {oh.fmt(i['mid'])} ₸" if i.get("mid") else "") + (f", короб ({i['boxQty']} шт) {oh.fmt(i['box'])} ₸" if i.get("box") else "") + f", розница {oh.fmt(i.get('rtl', 0))} ₸"
        rows.append(f"- {i['name']} ({i.get('brand') or '—'}) [id={i['id']}]: {p}; в наличии")
    return "\n".join(rows) or "По запросу товаров в наличии не найдено."


def transcribe(file_id):
    """Голосовое/аудио из Telegram -> текст (OpenAI)."""
    info = oh.tg("getFile", file_id=file_id)["result"]
    r = requests.get(f"https://api.telegram.org/file/bot{oh.BOT}/{info['file_path']}", timeout=40)
    r.raise_for_status()
    name = info["file_path"].rsplit("/", 1)[-1] or "voice.ogg"
    j = requests.post("https://api.openai.com/v1/audio/transcriptions", timeout=60,
                      headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                      files={"file": (name, r.content)}, data={"model": TRANSCRIBE_MODEL})
    if j.status_code >= 400:
        raise RuntimeError(f"OpenAI audio {j.status_code}: {j.text[:200]}")
    return (j.json().get("text") or "").strip()


def photo_data_url(file_id):
    if file_id.startswith("data:"):              # фото из пробного чата панели
        return file_id
    info = oh.tg("getFile", file_id=file_id)["result"]
    r = requests.get(f"https://api.telegram.org/file/bot{oh.BOT}/{info['file_path']}", timeout=30)
    r.raise_for_status()
    return "data:image/jpeg;base64," + base64.b64encode(r.content).decode()


def ai_reply(d, history, text, photo=None, ctx=None):
    """Ответ ИИ; возвращает (текст, нужен_менеджер). ctx — данные чата для оформления заказов (None — пробный чат)."""
    rules = get_setting(d, "rules", DEFAULT_RULES)
    kb = "\n\n".join(f"## {t}\n{b}" for t, b in d.run("SELECT title, body FROM kb ORDER BY id", many=True))
    system = f"{rules}\n\n{ORDER_RULES}\n\n{MEDIA_RULES}\n\n# База знаний\n{kb or '(пока пусто)'}\n\n# Товары из каталога по запросу клиента\n{product_context(text)}"
    msgs = [{"role": "system", "content": system}]
    for role, t in history[-12:]:
        msgs.append({"role": "user" if role == "client" else "assistant", "content": t})
    content = [{"type": "text", "text": text or "Клиент прислал фото."}]
    if photo:
        try:
            content.append({"type": "image_url", "image_url": {"url": photo_data_url(photo)}})
        except Exception as e:
            print("Фото клиента не загружено:", e, flush=True)
    msgs.append({"role": "user", "content": content})
    for step in range(4):                                         # модель может вызвать инструмент и затем ответить
        r = requests.post("https://api.openai.com/v1/chat/completions", timeout=45,
                          headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                          json={"model": OPENAI_MODEL, "messages": msgs, "temperature": 0.3, "max_tokens": 600,
                                "tools": ORDER_TOOL})
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:200]}")
        m = r.json()["choices"][0]["message"]
        calls = m.get("tool_calls") or []
        if not calls:
            out = (m.get("content") or "").strip()
            hand = HANDOFF in out
            return out.replace(HANDOFF, "").strip(), hand
        msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": calls})
        for c in calls:
            try:
                a_ = json.loads(c["function"]["arguments"] or "{}")
                fn = c["function"]["name"]
                res = (run_create_order(a_, ctx, history) if fn == "create_order"
                       else product_context(str(a_.get("query", ""))) if fn == "search_catalog" else "Неизвестный инструмент")
            except Exception as e:
                print("ИИ-заказ:", e, flush=True)
                oh.alert("aiorder", f"ИИ не смог оформить заказ: {str(e)[:250]}", every=300)
                res = "Ошибка при оформлении заказа. Передай диалог менеджеру (метка " + HANDOFF + ")."
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": res})
    return "Передал менеджеру — он поможет оформить заказ.", True


# ---------- входящие сообщения из Telegram ----------
def save_msg(d, cid, role, text, photo=None, unread=0):
    now = time.time()
    d.run("INSERT INTO msg (conv_id, role, text, photo, at) VALUES (%s,%s,%s,%s,%s)", (cid, role, text, photo, now))
    d.run("UPDATE conv SET last_at=%s, last_text=%s, unread=unread+%s WHERE id=%s", (now, (text or "[фото]")[:120], unread, cid))


def on_client_message(chat, user, text, photo=None, voice=None):
    """Вызывается из вебхука бота в отдельном потоке."""
    try:
        voice_failed = False
        if voice:                                          # голосовое -> текст, дальше как обычное сообщение
            try:
                if not OPENAI_KEY:
                    raise RuntimeError("нет ключа OpenAI")
                heard = transcribe(voice)
                text = "🎤 " + (heard or "(тишина)") + (f"\n{text}" if text else "")
            except Exception as e:
                print("Голосовое не расшифровано:", e, flush=True)
                text = "🎤 [голосовое сообщение, расшифровать не удалось]"
                voice_failed = True
        with _lock, db() as d:
            row = d.run("SELECT id, status FROM conv WHERE chat_id=%s", (str(chat),), one=True)
            name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "Клиент"
            if not row:
                cid = d.run("INSERT INTO conv (chat_id, name, username, status, unread, last_at) VALUES (%s,%s,%s,'ai',0,%s)",
                            (str(chat), name, user.get("username", ""), time.time()), ins=True)
                status = "ai"
            else:
                cid, status = row
            save_msg(d, cid, "client", text or "", photo, unread=1)
            hist = [(r, t) for r, t in d.run("SELECT role, text FROM msg WHERE conv_id=%s ORDER BY id DESC LIMIT 14", (cid,), many=True)][::-1][:-1]
            use_ai = status == "ai" and ai_on(d) and not voice_failed
            if status == "closed":
                d.run("UPDATE conv SET status=%s WHERE id=%s", ("ai", cid))
                status = "ai"
                use_ai = ai_on(d) and not voice_failed
        if status == "ai" and not use_ai:                  # ИИ выключен или нет ключа: диалог — менеджеру, клиенту короткий ответ
            ack = "Спасибо за сообщение! Передал менеджеру — он ответит в ближайшее время."
            oh.tg("sendMessage", chat_id=chat, text=ack)
            with _lock, db() as d:
                save_msg(d, cid, "ai", ack)
                d.run("UPDATE conv SET status='manager' WHERE id=%s", (cid,))
        if status == "manager" or not use_ai:
            oh.alert(f"inbox:{cid}", f"новое сообщение от {name}: {(text or '[фото]')[:200]}\nОтветьте в панели → Сообщения", every=300)
            return
        try:
            with db() as d:                       # без общей блокировки: ответ ИИ и заказ могут занять до минуты
                reply, hand = ai_reply(d, hist, text, photo, ctx={"chat": chat, "username": user.get("username", "")})
        except Exception as e:
            print("ИИ не ответил:", e, flush=True)
            oh.alert("ai", f"ИИ не ответил клиенту {name}: {str(e)[:200]}. Диалог передан менеджеру.", every=600)
            reply, hand = "Спасибо за сообщение! Передал менеджеру — он ответит в ближайшее время.", True
        oh.tg("sendMessage", chat_id=chat, text=reply)
        with _lock, db() as d:
            save_msg(d, cid, "ai", reply)
            if hand:
                d.run("UPDATE conv SET status='manager' WHERE id=%s", (cid,))
        if hand:
            oh.alert(f"inbox:{cid}", f"{name} ждёт менеджера: {(text or '[фото]')[:200]}\nОтветьте в панели → Сообщения", every=0)
    except Exception as e:
        print("Инбокс:", e, flush=True)
        oh.alert("inbox", f"инбокс не сохранил сообщение: {str(e)[:250]}", every=600)
