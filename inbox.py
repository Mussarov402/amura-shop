"""Инбокс AMURA: диалоги клиентов из Telegram-бота, ответы ИИ (OpenAI) и менеджера.
Хранение — Postgres (DATABASE_URL); без неё временный SQLite-файл (при перезапуске сервера пропадёт).
ИИ: OPENAI_API_KEY, модель OPENAI_MODEL (по умолчанию gpt-4o-mini)."""
import base64
import io
import json
import os
import re
import sqlite3
import threading
import time

import requests

import order_hook as oh
import wa

DB_URL = os.environ.get("DATABASE_URL", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
TRANSCRIBE_MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
HANDOFF = "[[MANAGER]]"
PG = bool(DB_URL)
_lock = threading.Lock()
_ready = [False]

DEFAULT_RULES = """Ты — менеджер по продажам оптового интернет-магазина корейской косметики AMURA (склад в Алматы, доставка по Казахстану).
Тон вежливый, тёплый и уважительный, но без панибратства и излишней восторженности: без смайликов и многословных комплиментов. Всегда обращайся к клиенту на «вы» (в том числе «Вы» в начале предложения не пиши с заглавной, если это не начало фразы). Здоровайся по-деловому: «Добрый день», «Здравствуйте», «Приветствую вас» — никогда не «привет» и не «приветик». На приветствие клиента отвечай, например: «Здравствуйте! Чем могу вам помочь?». Можно в меру использовать «пожалуйста» и «с удовольствием». Отвечай на языке клиента (русский или казахский), без выдумок: цены и наличие бери только из блока «Товары из каталога», остальное — из базы знаний.
Если не знаешь ответа, клиент просит скидку сверх правил, жалуется, спорит об оплате, хочет изменить или отменить заказ, или просит живого менеджера — коротко скажи, что передаёшь менеджеру, и добавь в самом конце ответа метку """ + HANDOFF + """.
Заказ можно оформить прямо в этом чате (см. раздел «Оформление заказа») или клиент оформит его сам на сайте."""


def clean_reply(t):
    """Убирает разметку и служебные id, которых клиент видеть не должен."""
    t = re.sub(r"\s*\[id=[^\]]*\]", "", t or "")
    t = t.replace("**", "").replace("__", "")
    return re.sub(r"(?m)^#+\s*", "", t).strip()


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
            "CREATE TABLE IF NOT EXISTS staff (id " + pk + ", chat_id TEXT UNIQUE, name TEXT, username TEXT, perms TEXT, notify INTEGER DEFAULT 1, active INTEGER DEFAULT 1, at DOUBLE PRECISION)",
            "CREATE TABLE IF NOT EXISTS invite (code TEXT PRIMARY KEY, name TEXT, perms TEXT, notify INTEGER DEFAULT 1, at DOUBLE PRECISION)",
        ):
            d.run(s)
        d.c.commit()
        try:                                              # вложения диалога (голос, файлы, видео)
            d.run("ALTER TABLE msg ADD COLUMN media TEXT")
            d.c.commit()
        except Exception:
            d.c.rollback()
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
Когда клиент хочет заказать, по умолчанию предложи оформить заказ на сайте — там весь каталог, актуальные цены и накладная создаётся сразу. Скажи это коротко и по-человечески и дай ссылку: SITE_LINK. Но если клиенту удобнее иначе (присылает список текстом, фото полки или упаковок, голосовое, говорит «давайте тут», «лень на сайте», не может зайти) — не настаивай на сайте и оформи заказ сам, как описано ниже.
Ты можешь оформить заказ в МойСклад инструментом create_order. Порядок строго такой:
1. Собери у клиента: какие товары и сколько (бери id из блока «Товары из каталога»), имя, телефон, город, способ отправки (kamaz КАМАЗ, rail ЖД, avia Авиа — для них нужна логистическая компания; kazpost Казпочта — нужны ФИО получателя, индекс из 6 цифр и адрес; courier курьер по Алматы — нужен адрес; pickup самовывоз).
2. Покажи клиенту сводку: товары, количество, цену за штуку, итог с учётом комиссии банка 0,95% (и услуги грузчика 1 000 ₸ для КАМАЗ/ЖД/Авиа), способ отправки, имя, телефон, город. Строку с итогом начни словом «Итого».
3. Только после явного согласия клиента («да», «подтверждаю», «оформляй») вызови create_order с client_confirmed=true. Никогда не оформляй заказ без подтверждения сводки.
4. Если заказ не удалось создать или сумма очень большая — не пытайся ещё раз, передай менеджеру (метка """ + HANDOFF + """).
После успешного заказа накладная и реквизиты оплаты уходят клиенту автоматически — просто коротко подтверди номер заказа."""

MEDIA_RULES = """# Фото и голосовые
Клиент может прислать фото или голосовое (оно уже расшифровано и помечено 🎤). Относись к ним как к обычному тексту.
- Фото товара или упаковки: прочитай бренд и название на упаковке, найди товар инструментом search_catalog и назови цену и наличие. Если уверенно определить не получилось — честно скажи и попроси название или артикул.
- PDF-чек (в сообщении видно «📄 PDF» и текст внутри) — то же самое, что фото чека. Если текст прочитать не удалось, так и скажи и передай менеджеру.
- Фото чека об оплате, скриншот перевода или слова «оплатил(а)»: оплату сам не проверяй и не подтверждай — у тебя нет доступа к банку. Ответь коротко и по-человечески, что передал чек менеджеру на проверку и сообщите, когда оплата подтвердится (метка """ + HANDOFF + """). Если не ясно, за какой заказ оплата, спроси номер заказа. Сумму и получателя с чека не озвучивай и ничего не обещай по срокам отгрузки.
- Файл любого типа (в сообщении «📎 Файл»), видео («🎥 Видео») — ты их открыть не можешь: коротко скажи, что передал менеджеру, и добавь метку """ + HANDOFF + """.
- Фото брака, повреждения, жалобы: извинись, попроси номер заказа и передай менеджеру (метка """ + HANDOFF + """).
- Фото, на котором нет ничего про заказ или товары, — вежливо уточни, чем помочь.
Никогда не выдумывай то, чего не видно на фото."""

SEARCH_RULES = """# Поиск товаров
Названия брендов и товаров в каталоге написаны латиницей (Celimax, Axis-Y, Median). Поиск идёт по названию, описанию, бренду, группе, артикулу и штрихкоду. Клиент часто пишет по-русски («Селимакс», «тонер серый»): сам переведи в латиницу и вызови search_catalog, например query="Celimax toner". Если первый поиск пуст — попробуй ещё раз по бренду или по другому слову. Когда клиент спрашивает о категории или ингредиенте («центелла», «тонеры», «солнцезащитный»), вызови search_catalog несколько раз с разными словами и синонимами (Centella, Madecassoside, Cica) и покажи все найденные подходящие товары. На «ещё что-нибудь» или «другое» повтори поиск шире и не повторяй уже названные товары. Нельзя говорить «каталог недоступен» или «товара нет», пока не вызван search_catalog; говори, что товара нет, только если поиск реально ничего не вернул."""

PRICE_RULES = """# Цены — только из каталога
Цену и наличие называй только из блока «Товары из каталога» или результата search_catalog. Никогда не называй цену по памяти, по догадке и не подтверждай цену, которую назвал клиент («по 3100 нужен» — не значит, что так и есть).
Если клиент называет свою цену или просит конкретный товар «по такой-то цене» — найди товар и скажи фактическую цену из каталога, даже если она выше. Если товара нет в данных — сначала вызови search_catalog.
Если по запросу подходит несколько товаров (например, несколько тонеров бренда), не выбирай сам: назови подходящие варианты с их ценами или спроси, какой именно. Название товара называй только точно как в каталоге.
Скидок и особых цен не обещай: цена — из каталога, любые другие условия только через менеджера (метка """ + HANDOFF + """)."""

STYLE_RULES = """# Как писать клиенту
Пиши как живой менеджер в мессенджере: естественно, своими словами, коротко, на «вы». Никакой разметки (**, #, скобок) и никаких id товаров.
Отвечай прямо на вопрос: «Да, Lagom Micro Foam Cleanser есть, 2 750 ₸ за штуку, в наличии». Не пиши «Вот информация:», «Вот что есть:» и подобные канцелярские вставки.
Один товар — одним предложением, без списка. Список (с новой строки, через дефис) только когда товаров три и больше, цену пиши обычным текстом: «Anua Peach 77 Cream — 6 250 ₸».
Цены называй как оптовые («2 750 ₸ за штуку»); слово «опт» и розницу упоминай, только если спросили. Оптовые скидки (от 10 штук, коробом) — одной фразой, если уместно.
Не повторяй одну и ту же концовку. Не заканчивай шаблонами вроде «Если у вас есть вопросы, дайте знать!» и «Что вас интересует?». Вместо этого одной короткой живой фразой веди к делу: «Сколько штук нужно?», «Оформим?», «Нужно что-то ещё?» — или вообще ничего, если вопрос закрыт.
Если по фото не уверен, тот ли товар, скажи так: «Похоже на …, это он?» и предложи 1–3 ближайших варианта.
Если бренда или товара в каталоге нет (поиск ничего не вернул) — скажи просто: «Такого у нас нет» и не подсовывай случайные товары. Аналоги предлагай, только когда понятно, что нужно клиенту, и объясняй одной фразой, чем подходит. Если не понял, что за бренд, спроси название латиницей или попроси фото."""

SEARCH_TOOL = {"type": "function", "function": {
    "name": "search_catalog",
    "description": "Найти товары в каталоге по названию, описанию, бренду, группе, артикулу или штрихкоду. Запрос пиши латиницей, как в каталоге (Celimax toner). Возвращает id, цены и наличие.",
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
    if wa.is_wa(chat) and not re.sub(r"\D", "", d["phone"]):
        d["phone"] = wa.number(chat)                # номер из WhatsApp — контакт по умолчанию
    key = "tg:" + chat + ":" + oh.hashlib.sha1(json.dumps([items, d["shipping"], time.strftime("%Y%m%d")], sort_keys=True).encode()).hexdigest()[:16]
    for k in [k for k, v in _ai_done.items() if now - v[0] > 600]:
        _ai_done.pop(k, None)
    if key in _ai_done:
        return _ai_done[key][1]
    payload, status = oh.order_core(d, key, "tg:" + chat, None, source=f"из {channel_name(chat)} (ИИ-продажник)")
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
        send_pdf(chat, f"AMURA-{data['number']}.pdf", pdf, f"Ваш заказ AMURA № {data['number']} на {oh.fmt(data['total'])} ₸.")
        send_text(chat, oh.pay_text())
    except Exception as e:
        print("ИИ-заказ: накладная не ушла клиенту:", e, flush=True)
        return f"Заказ № {data['number']} создан на {oh.fmt(data['total'])} ₸, но накладную отправит менеджер. Сообщи клиенту номер заказа."
    return f"Заказ № {data['number']} создан на сумму {oh.fmt(data['total'])} ₸. Накладная и реквизиты оплаты отправлены клиенту в чат."


# ---------- ИИ ----------
_TR = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e|yo", "ж": "zh|j", "з": "z", "и": "i", "й": "y|i",
       "к": "k|c", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s|c", "т": "t", "у": "u", "ф": "f|ph",
       "х": "h|kh|x", "ц": "c|ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y|i", "ь": "", "э": "e", "ю": "yu|u", "я": "ya|a"}


def _stem(w):
    """Основа слова: «центеллой» -> «центел», «тонеры» -> «тоне» (латиницу не трогаем)."""
    if re.search(r"[а-яё]", w):
        n = len(w)
        return w[:-3] if n >= 8 else w[:-2] if n >= 6 else w[:-1] if n == 5 else w
    return w


def _forms(w):
    """Формы слова для поиска: основа и (для русских слов) её варианты латиницей: центел -> centel, tsentel."""
    s = _stem(w)
    out = {s}
    if re.search(r"[а-яё]", s):
        outs = [""]
        for ch in s.replace("кс", "x"):
            opts = _TR.get(ch, ch if ch.isalnum() else "").split("|") if ch != "x" else ["x"]
            outs = [o + p for o in outs for p in opts][:24]
        out |= set(outs)
    out |= {f.replace("kva", "qua").replace("cva", "qua") for f in out if "kva" in f or "cva" in f}      # аква -> aqua
    return {f for f in out if len(f) >= 2}


def _words(t):
    return [w for w in re.findall(r"[\w-]{2,}", t.lower())][:10]


def catalog_items():
    """Каталог из памяти сервера; если его ещё нет (после перезапуска) — загружаем из МойСклад."""
    v = oh._cache.get("live")
    if v:
        return v[1]["items"]
    try:
        return oh.live()["items"]
    except Exception as e:
        print("ИИ: каталог не загружен:", e, flush=True)
        return None


def brand_list(items):
    return ", ".join(sorted({i.get("brand") for i in items if i.get("brand")}, key=str.lower))[:2500]


def product_context(text, limit=8):
    items = catalog_items()
    if items is None:
        return "Каталог сейчас не загрузился. Скажи клиенту, что уточнишь наличие и цену, и передай менеджеру (метка " + HANDOFF + ")."
    ws = oh.PUBLIC_WHOLESALE
    ws_words = [_forms(w) for w in _words(text)]
    descs = (oh._cache.get("descs") or (0, {}))[1]
    scored = []
    for i in items:
        name = i["name"].lower()
        tags = " ".join(str(i.get(k) or "") for k in ("brand", "group", "country")).lower()      # бренд, группа, страна
        ids = " ".join(str(i.get(k) or "") for k in ("code", "article", "barcode")).lower()
        desc = (descs.get(i["id"]) or i.get("desc") or "").lower()                                  # описание
        sc = sum(3 * any(f in name for f in w) + 2 * any(f in tags for f in w) + 2 * any(f in ids for f in w) + any(f in desc for f in w) for w in ws_words)
        if sc:
            scored.append((sc, i))
    scored.sort(key=lambda x: -x[0])
    rows = []
    for _, i in scored[:limit]:
        p = f"розница {oh.fmt(i.get('rtl', 0))} ₸"
        if ws:
            p = f"опт {oh.fmt(i.get('opt', 0))} ₸" + (f", от 10 шт {oh.fmt(i['mid'])} ₸" if i.get("mid") else "") + (f", короб ({i['boxQty']} шт) {oh.fmt(i['box'])} ₸" if i.get("box") else "") + f", розница {oh.fmt(i.get('rtl', 0))} ₸"
        grp = f"; группа: {i['group']}" if i.get("group") else ""
        rows.append(f"- {i['name']} ({i.get('brand') or '—'}{grp}) [id={i['id']}]: {p}; в наличии")
    return "\n".join(rows) or "По этому запросу ничего не найдено. Попробуй вызвать search_catalog с названием латиницей, как в каталоге."


def fetch_file(file_id, timeout=40):
    """Файл из Telegram или WhatsApp (id вида «wa:...») -> (байты, имя файла)."""
    if str(file_id).startswith("wa:"):
        data, mime = wa.download(file_id)
        return data, "voice.ogg" if "ogg" in mime else "file." + (mime.split("/")[-1].split(";")[0] or "bin")
    info = oh.tg("getFile", file_id=file_id)["result"]
    r = requests.get(f"https://api.telegram.org/file/bot{oh.BOT}/{info['file_path']}", timeout=timeout)
    r.raise_for_status()
    return r.content, info["file_path"].rsplit("/", 1)[-1]


def send_text(chat, text):
    if wa.is_wa(chat):
        wa.send_text(chat, text)
    else:
        oh.tg("sendMessage", chat_id=chat, text=text)


def send_pdf(chat, name, pdf, caption):
    if wa.is_wa(chat):
        wa.send_media(chat, "document", pdf, name, "application/pdf", caption)
    else:
        oh.tg("sendDocument", chat_id=chat, caption=caption, _files={"document": (name, pdf, "application/pdf")})


def channel_name(chat):
    return "WhatsApp" if wa.is_wa(chat) else "Telegram"


def transcribe(file_id):
    """Голосовое/аудио -> текст (OpenAI)."""
    data, name = fetch_file(file_id)
    j = requests.post("https://api.openai.com/v1/audio/transcriptions", timeout=60,
                      headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                      files={"file": (name or "voice.ogg", data)}, data={"model": TRANSCRIBE_MODEL})
    if j.status_code >= 400:
        raise RuntimeError(f"OpenAI audio {j.status_code}: {j.text[:200]}")
    return (j.json().get("text") or "").strip()


def pdf_text(file_id, size=0):
    """Текст из PDF-чека (банковские и Kaspi-чеки — текстовые). Пусто, если это скан или файл слишком большой."""
    if size and size > 8_000_000:
        return ""
    data, _ = fetch_file(file_id, 30)
    from pypdf import PdfReader
    rd = PdfReader(io.BytesIO(data))
    txt = "\n".join((p.extract_text() or "") for p in rd.pages[:3])
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", txt)).strip()[:3000]


def photo_data_url(file_id):
    if file_id.startswith("data:"):              # фото из пробного чата панели
        return file_id
    data, _ = fetch_file(file_id, 30)
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def ai_reply(d, history, text, photo=None, ctx=None):
    """Ответ ИИ; возвращает (текст, нужен_менеджер). ctx — данные чата для оформления заказов (None — пробный чат)."""
    rules = get_setting(d, "rules", DEFAULT_RULES)
    kb = "\n\n".join(f"## {t}\n{b}" for t, b in d.run("SELECT title, body FROM kb ORDER BY id", many=True))
    recent = " ".join([text or ""] + [x for r_, x in history[-6:] if r_ == "client"][-2:])      # слова текущего и двух прошлых сообщений клиента (текущее — первым)
    cat = catalog_items()
    brands = brand_list(cat) if cat else ""
    order_rules = ORDER_RULES.replace("SITE_LINK", oh.SITE_URL or "https://mussarov402.github.io/amura-shop")
    system = f"{rules}\n\n{order_rules}\n\n{MEDIA_RULES}\n\n{STYLE_RULES}\n\n{PRICE_RULES}\n\n{SEARCH_RULES}\n\n# Бренды в каталоге\n{brands or '(каталог не загружен)'}\n\n# База знаний\n{kb or '(пока пусто)'}\n\n# Товары из каталога по запросу клиента\n{product_context(recent)}"
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
                       else product_context(str(a_.get("query", "")), limit=15) if fn == "search_catalog" else "Неизвестный инструмент")
            except Exception as e:
                print("ИИ-заказ:", e, flush=True)
                oh.alert("aiorder", f"ИИ не смог оформить заказ: {str(e)[:250]}", every=300)
                res = "Ошибка при оформлении заказа. Передай диалог менеджеру (метка " + HANDOFF + ")."
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": res})
    return "Передал менеджеру — он поможет оформить заказ.", True


# ---------- входящие сообщения из Telegram ----------
MEDIA_LABEL = {"voice": "🎤 Голосовое", "audio": "🎵 Аудио", "video": "🎥 Видео", "doc": "📎 Файл"}


def save_msg(d, cid, role, text, photo=None, unread=0, media=None):
    now = time.time()
    d.run("INSERT INTO msg (conv_id, role, text, photo, media, at) VALUES (%s,%s,%s,%s,%s,%s)",
          (cid, role, text, photo, json.dumps(media, ensure_ascii=False) if media else None, now))
    last = text or (MEDIA_LABEL.get((media or {}).get("t"), "[фото]"))
    d.run("UPDATE conv SET last_at=%s, last_text=%s, unread=unread+%s WHERE id=%s", (now, last[:120], unread, cid))


def note_invoice(chat, user, number, total, doc_id):
    """Клиент получил накладную в Telegram-боте: диалог появляется в «Сообщениях», можно сразу написать клиенту из панели."""
    with _lock, db() as d:
        row = d.run("SELECT id FROM conv WHERE chat_id=%s", (str(chat),), one=True)
        name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "Клиент"
        if not row:
            cid = d.run("INSERT INTO conv (chat_id, name, username, status, unread, last_at) VALUES (%s,%s,%s,'ai',0,%s)",
                        (str(chat), name, user.get("username", ""), time.time()), ins=True)
        else:
            cid = row[0]
        media = {"t": "doc", "id": doc_id, "name": f"AMURA-{number}.pdf", "size": 0, "mime": "application/pdf"} if doc_id else None
        save_msg(d, cid, "manager", f"🧾 Накладная по заказу № {number} на {oh.fmt(total)} ₸ и реквизиты отправлены клиенту", media=media)
    return cid


def on_manager_echo(chat, text):
    """Менеджер ответил клиенту прямо с телефона (приложение WhatsApp Business): показываем в панели и переводим диалог к менеджеру (ИИ молчит)."""
    with _lock, db() as d:
        row = d.run("SELECT id FROM conv WHERE chat_id=%s", (str(chat),), one=True)
        if not row:
            cid = d.run("INSERT INTO conv (chat_id, name, username, status, unread, last_at) VALUES (%s,%s,%s,'manager',0,%s)",
                        (str(chat), "+" + wa.number(chat), "", time.time()), ins=True)
        else:
            cid = row[0]
        save_msg(d, cid, "manager", text or "")
        d.run("UPDATE conv SET status='manager' WHERE id=%s", (cid,))


def on_client_message(chat, user, text, photo=None, voice=None, pdf=None, media=None):
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
        if pdf:                                            # PDF (чаще всего чек): достаём текст и дальше как обычное сообщение
            try:
                body = pdf_text(pdf["id"], pdf.get("size", 0))
            except Exception as e:
                print("PDF не прочитан:", e, flush=True)
                body = ""
            text = f"📄 PDF «{pdf['name']}»" + (f"\n{body}" if body else " (текст прочитать не удалось)") + (f"\n{text}" if text else "")
        with _lock, db() as d:
            row = d.run("SELECT id, status FROM conv WHERE chat_id=%s", (str(chat),), one=True)
            name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "Клиент"
            if not row:
                cid = d.run("INSERT INTO conv (chat_id, name, username, status, unread, last_at) VALUES (%s,%s,%s,'ai',0,%s)",
                            (str(chat), name, user.get("username", ""), time.time()), ins=True)
                status = "ai"
            else:
                cid, status = row
            save_msg(d, cid, "client", text or "", photo, unread=1, media=media)
        try:                                               # push в панель: звук и счётчик на иконке
            import push
            push.notify(f"💬 {name}", text or MEDIA_LABEL.get((media or {}).get("t"), "[фото]"), f"/admin#inbox/{cid}", perm="inbox", tag=f"c{cid}")
        except Exception as e:
            print("Push о сообщении:", e, flush=True)
        with _lock, db() as d:
            hist = [(r, t) for r, t in d.run("SELECT role, text FROM msg WHERE conv_id=%s ORDER BY id DESC LIMIT 14", (cid,), many=True)][::-1][:-1]
            use_ai = status == "ai" and ai_on(d) and not voice_failed
            if status == "closed":
                d.run("UPDATE conv SET status=%s WHERE id=%s", ("ai", cid))
                status = "ai"
                use_ai = ai_on(d) and not voice_failed
        if status == "ai" and not use_ai:                  # ИИ выключен или нет ключа: диалог — менеджеру, клиенту короткий ответ
            ack = "Спасибо за сообщение! Передал менеджеру — он ответит в ближайшее время."
            send_text(chat, ack)
            with _lock, db() as d:
                save_msg(d, cid, "ai", ack)
                d.run("UPDATE conv SET status='manager' WHERE id=%s", (cid,))
        if status == "manager" or not use_ai:
            if oh.notif_on("msg"):
                oh.notify_staff(f"inbox:{cid}", f"💬 Новое сообщение ({channel_name(chat)}) от {name}: {(text or '[фото]')[:200]}\nОтветьте в панели → Сообщения", every=300)
            return
        try:
            with db() as d:                       # без общей блокировки: ответ ИИ и заказ могут занять до минуты
                reply, hand = ai_reply(d, hist, text, photo, ctx={"chat": chat, "username": user.get("username", ""), "channel": channel_name(chat)})
        except Exception as e:
            print("ИИ не ответил:", e, flush=True)
            oh.alert("ai", f"ИИ не ответил клиенту {name}: {str(e)[:200]}. Диалог передан менеджеру.", every=600)
            reply, hand = "Спасибо за сообщение! Передал менеджеру — он ответит в ближайшее время.", True
        reply = clean_reply(reply)
        send_text(chat, reply)
        with _lock, db() as d:
            save_msg(d, cid, "ai", reply)
            if hand:
                d.run("UPDATE conv SET status='manager' WHERE id=%s", (cid,))
        if hand:                                       # в системе — всегда: push «ждёт менеджера»
            try:
                import push
                push.notify(f"🙋 {name} ждёт менеджера", text or "[фото]", f"/admin#inbox/{cid}", perm="inbox", tag=f"h{cid}")
            except Exception as e:
                print("Push «ждёт менеджера»:", e, flush=True)
        if hand and oh.notif_on("handoff"):
            oh.notify_staff(f"inbox:{cid}", f"🙋 {name} ({channel_name(chat)}) ждёт менеджера: {(text or '[фото]')[:200]}\nОтветьте в панели → Сообщения")
            if pdf and oh.OWNER:
                try:
                    if wa.is_wa(chat):                  # из WhatsApp файл отправляем байтами
                        oh.notify_staff(f"pdf:{cid}", f"PDF от {name} (WhatsApp, передано менеджеру)", method="sendDocument", _files={"document": (pdf["name"], fetch_file(pdf["id"])[0], "application/pdf")})
                    else:
                        oh.notify_staff(f"pdf:{cid}", f"PDF от {name} (передано менеджеру)", method="sendDocument", document=pdf["id"])
                except Exception as e:
                    print("PDF владельцу не ушёл:", e, flush=True)
            if photo and oh.OWNER:                      # чек или фото брака — сразу владельцу, без захода в панель
                try:
                    if wa.is_wa(chat):
                        oh.notify_staff(f"photo:{cid}", f"Фото от {name} (WhatsApp, передано менеджеру)", method="sendPhoto", _files={"photo": ("photo.jpg", fetch_file(photo)[0], "image/jpeg")})
                    else:
                        oh.notify_staff(f"photo:{cid}", f"Фото от {name} (передано менеджеру)", method="sendPhoto", photo=photo)
                except Exception as e:
                    print("Фото владельцу не ушло:", e, flush=True)
    except Exception as e:
        print("Инбокс:", e, flush=True)
        oh.alert("inbox", f"инбокс не сохранил сообщение: {str(e)[:250]}", every=600)
