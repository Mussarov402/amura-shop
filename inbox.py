"""Инбокс AMURA: диалоги клиентов из Telegram-бота, ответы ИИ (OpenAI) и менеджера.
Хранение — Postgres (DATABASE_URL); без неё временный SQLite-файл (при перезапуске сервера пропадёт).
ИИ: OPENAI_API_KEY, модель OPENAI_MODEL (по умолчанию gpt-4o-mini)."""
import base64
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
HANDOFF = "[[MANAGER]]"
PG = bool(DB_URL)
_lock = threading.Lock()
_ready = [False]

DEFAULT_RULES = """Ты — менеджер по продажам оптового интернет-магазина корейской косметики AMURA (склад в Алматы, доставка по Казахстану).
Отвечай коротко, дружелюбно, на языке клиента (русский или казахский), без выдумок: цены и наличие бери только из блока «Товары из каталога», остальное — из базы знаний.
Если не знаешь ответа, клиент просит скидку сверх правил, жалуется, спорит об оплате, хочет изменить или отменить заказ, или просит живого менеджера — коротко скажи, что передаёшь менеджеру, и добавь в самом конце ответа метку """ + HANDOFF + """.
Оформить заказ клиент может на сайте, реквизиты оплаты бот присылает после заказа."""


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
        rows.append(f"- {i['name']} ({i.get('brand') or '—'}): {p}; в наличии")
    return "\n".join(rows) or "По запросу товаров в наличии не найдено."


def photo_data_url(file_id):
    info = oh.tg("getFile", file_id=file_id)["result"]
    r = requests.get(f"https://api.telegram.org/file/bot{oh.BOT}/{info['file_path']}", timeout=30)
    r.raise_for_status()
    return "data:image/jpeg;base64," + base64.b64encode(r.content).decode()


def ai_reply(d, history, text, photo=None):
    """Ответ ИИ; возвращает (текст, нужен_менеджер)."""
    rules = get_setting(d, "rules", DEFAULT_RULES)
    kb = "\n\n".join(f"## {t}\n{b}" for t, b in d.run("SELECT title, body FROM kb ORDER BY id", many=True))
    system = f"{rules}\n\n# База знаний\n{kb or '(пока пусто)'}\n\n# Товары из каталога по запросу клиента\n{product_context(text)}"
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
    r = requests.post("https://api.openai.com/v1/chat/completions", timeout=45,
                      headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                      json={"model": OPENAI_MODEL, "messages": msgs, "temperature": 0.3, "max_tokens": 500})
    if r.status_code >= 400:
        raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:200]}")
    out = r.json()["choices"][0]["message"]["content"].strip()
    hand = HANDOFF in out
    return out.replace(HANDOFF, "").strip(), hand


# ---------- входящие сообщения из Telegram ----------
def save_msg(d, cid, role, text, photo=None, unread=0):
    now = time.time()
    d.run("INSERT INTO msg (conv_id, role, text, photo, at) VALUES (%s,%s,%s,%s,%s)", (cid, role, text, photo, now))
    d.run("UPDATE conv SET last_at=%s, last_text=%s, unread=unread+%s WHERE id=%s", (now, (text or "[фото]")[:120], unread, cid))


def on_client_message(chat, user, text, photo=None):
    """Вызывается из вебхука бота в отдельном потоке."""
    try:
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
            use_ai = status == "ai" and ai_on(d)
            if status == "closed":
                d.run("UPDATE conv SET status=%s WHERE id=%s", ("ai", cid))
                status = "ai"
                use_ai = ai_on(d)
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
            with _lock, db() as d:
                reply, hand = ai_reply(d, hist, text, photo)
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
