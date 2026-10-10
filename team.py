"""Команда: сотрудники с доступом в панель и уведомлениями. Хранится в базе инбокса (таблицы staff, invite).
Владелец приглашает сотрудника в панели («Обзор» → «Команда»): ссылка t.me/<бот>?start=stf_<код> одноразовая и живёт 7 дней."""
import json
import secrets
import threading
import time

import inbox

PERMS = {"inbox": "Сообщения", "orders": "Заказы", "products": "Товары", "warehouse": "Склад"}
INVITE_TTL = 7 * 86400
_lock = threading.Lock()
_cache = {"t": 0.0, "rows": []}


def _perms(v):
    return [p for p in (v or []) if p in PERMS]


def _row(r):
    return {"id": r[0], "chat": r[1], "name": r[2] or "", "username": r[3] or "", "perms": _perms(json.loads(r[4] or "[]")),
            "notify": bool(r[5]), "active": bool(r[6]), "at": r[7]}


def staff(force=False):
    """Все сотрудники (кэш 20 с; при ошибке базы — последний известный список)."""
    if not force and time.time() - _cache["t"] < 20:
        return _cache["rows"]
    try:
        with inbox.db() as d:
            rows = [_row(r) for r in d.run("SELECT id, chat_id, name, username, perms, notify, active, at FROM staff ORDER BY id", many=True)]
        _cache.update(t=time.time(), rows=rows)
    except Exception as e:
        print("Сотрудники не загружены:", e, flush=True)
    return _cache["rows"]


def by_chat(chat):
    for s in staff():
        if s["chat"] == str(chat) and s["active"]:
            return s
    return None


def notify_chats():
    return [s["chat"] for s in staff() if s["active"] and s["notify"]]


def invites():
    now = time.time()
    with inbox.db() as d:
        d.run("DELETE FROM invite WHERE at < %s", (now - INVITE_TTL,))
        return [{"code": c, "name": n or "", "perms": _perms(json.loads(p or "[]")), "notify": bool(nt), "at": a}
                for c, n, p, nt, a in d.run("SELECT code, name, perms, notify, at FROM invite ORDER BY at DESC", many=True)]


def create_invite(name, perms, notify):
    code = secrets.token_urlsafe(12).replace("-", "a").replace("_", "b")
    with inbox.db() as d:
        d.run("INSERT INTO invite (code, name, perms, notify, at) VALUES (%s,%s,%s,%s,%s)",
              (code, (name or "").strip()[:60], json.dumps(_perms(perms)), 1 if notify else 0, time.time()))
    return code


def remove_invite(code):
    with inbox.db() as d:
        d.run("DELETE FROM invite WHERE code=%s", (code,))


def accept_invite(code, chat, user):
    """Сотрудник открыл ссылку-приглашение. Возвращает его запись или None, если ссылка недействительна."""
    with _lock, inbox.db() as d:
        r = d.run("SELECT name, perms, notify, at FROM invite WHERE code=%s", (code,), one=True)
        if not r or time.time() - r[3] > INVITE_TTL:
            return None
        name = r[0] or " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or user.get("username") or "Сотрудник"
        d.run("DELETE FROM invite WHERE code=%s", (code,))
        if d.run("SELECT 1 FROM staff WHERE chat_id=%s", (str(chat),), one=True):
            d.run("UPDATE staff SET name=%s, username=%s, perms=%s, notify=%s, active=1 WHERE chat_id=%s",
                  (name, user.get("username", ""), r[1], r[2], str(chat)))
        else:
            d.run("INSERT INTO staff (chat_id, name, username, perms, notify, active, at) VALUES (%s,%s,%s,%s,%s,1,%s)",
                  (str(chat), name, user.get("username", ""), r[1], r[2], time.time()))
    staff(force=True)
    return by_chat(chat)


def update(sid, data):
    sets, args = [], []
    if "perms" in data:
        sets.append("perms=%s"); args.append(json.dumps(_perms(data["perms"])))
    if "notify" in data:
        sets.append("notify=%s"); args.append(1 if data["notify"] else 0)
    if "active" in data:
        sets.append("active=%s"); args.append(1 if data["active"] else 0)
    if "name" in data:
        sets.append("name=%s"); args.append(str(data["name"]).strip()[:60])
    if sets:
        with inbox.db() as d:
            d.run(f"UPDATE staff SET {', '.join(sets)} WHERE id=%s", (*args, sid))
    staff(force=True)


def remove(sid):
    with inbox.db() as d:
        d.run("DELETE FROM staff WHERE id=%s", (sid,))
    staff(force=True)
