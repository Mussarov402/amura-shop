"""Группы контактов и рассылки в «Сообщениях».
В группу можно добавить любой диалог — Telegram, WhatsApp, Instagram, чат сайта — и отправить всем одно сообщение (текст, фото или файл).
Ограничения мессенджеров: WhatsApp и Instagram разрешают писать первым только в течение 24 часов после последнего сообщения клиента —
остальным рассылка не уходит (помечаются «пропущено» с причиной). Telegram и чат сайта — без ограничений.
В тексте можно написать {имя} — подставится имя клиента."""
import threading
import time

from flask import Blueprint, jsonify, request

import admin
import inbox
import order_hook as oh

bp = Blueprint("groups", __name__)
DAY = 86400
_ready = [False]
PAUSE = 0.12              # между сообщениями: Telegram — не больше ~30 в секунду, WhatsApp — щадящий темп


def db():
    d = inbox.db()
    if not _ready[0]:
        pk = "SERIAL PRIMARY KEY" if inbox.PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
        for s in (f"CREATE TABLE IF NOT EXISTS grp (id {pk}, name TEXT, at DOUBLE PRECISION)",
                  "CREATE TABLE IF NOT EXISTS grp_member (grp_id INTEGER, conv_id INTEGER, at DOUBLE PRECISION, PRIMARY KEY (grp_id, conv_id))",
                  # рассылка: state — run | done; счётчики обновляются по ходу
                  f"CREATE TABLE IF NOT EXISTS bcast (id {pk}, grp_id INTEGER, text TEXT, file TEXT, by_name TEXT, at DOUBLE PRECISION, "
                  "total INTEGER DEFAULT 0, sent INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, state TEXT)",
                  # получатель: state — wait | sent | skip | fail, error — причина
                  "CREATE TABLE IF NOT EXISTS bcast_item (bcast_id INTEGER, conv_id INTEGER, state TEXT, error TEXT, PRIMARY KEY (bcast_id, conv_id))"):
            d.run(s)
        d.c.commit()
        _ready[0] = True
    return d


def window(chat, last_client):
    """Можно ли написать клиенту первым. Возвращает (можно, причина)."""
    ch = inbox.channel(chat)
    if ch in ("wa", "ig"):
        if not last_client or time.time() - last_client > DAY - 60:
            return False, ("WhatsApp" if ch == "wa" else "Instagram") + ": больше 24 часов с последнего сообщения клиента"
    return True, ""


def first_name(name):
    w = [x for x in str(name or "").replace("(", " ").split() if not x.lstrip("+").isdigit()]
    return w[0] if w else ""


def _contacts(d, ids=None):
    """Диалоги с последним сообщением клиента: [(id, name, username, chat_id, last_client)]."""
    sql = ("SELECT c.id, c.name, c.username, c.chat_id, (SELECT MAX(m.at) FROM msg m WHERE m.conv_id=c.id AND m.role='client') "
           "FROM conv c")
    if ids is not None:
        if not ids:
            return []
        sql += " WHERE c.id IN (" + ",".join(str(int(i)) for i in ids) + ")"
    return d.run(sql + " ORDER BY c.last_at DESC LIMIT 3000", many=True) or []


def _card(r):
    cid, name, username, chat, last = r
    ok, why = window(chat, last)
    ch = inbox.channel(chat)
    return {"id": cid, "name": name or "Без имени", "username": username or "", "channel": ch,
            "phone": admin.wa.number(chat) if ch == "wa" else "", "open": ok, "why": why}


@bp.get("/admin/api/inbox/contacts")
@admin.need("inbox")
def contacts():
    """Все диалоги для выбора в группу: поиск по имени, @нику и номеру, фильтр по мессенджеру."""
    q, ch = request.args.get("q", "").strip().lower(), request.args.get("ch", "")
    with db() as d:
        rows = [_card(r) for r in _contacts(d)]
    if ch:
        rows = [r for r in rows if r["channel"] == ch]
    if q:
        rows = [r for r in rows if q in (r["name"] + " " + r["username"] + " " + r["phone"]).lower()]
    return jsonify(ok=True, contacts=rows[:500])


def _cp_chat(cp):
    """Куда писать клиенту из МойСклад: Telegram (если входил на сайт через Telegram — бот может писать всегда), иначе WhatsApp по номеру."""
    attrs = cp.get("attributes") or []
    tg = next((str(a.get("value") or "").strip() for a in attrs if a.get("name") == oh.ATTR_TGID), "")
    if tg.lstrip("-").isdigit():
        return tg
    ph = oh.norm_phone(cp.get("phone", ""))
    return "wa:" + ph if len(ph) == 11 and ph.startswith("7") else None


def _cp_card(cp):
    chat = _cp_chat(cp)
    return {"id": cp["id"], "name": cp.get("name") or "Без имени", "phone": oh.norm_phone(cp.get("phone", "")),
            "channel": inbox.channel(chat) if chat else ""}


def _ms_clients(q=""):
    """Покупатели из МойСклад (без архивных): поиск — первые 100, без поиска — все, постранично по 1000."""
    if q:
        return oh.ms("GET", "/entity/counterparty", params={"search": q, "limit": 100}, timeout=20)["rows"]
    out, off = [], 0
    while True:
        rows = oh.ms("GET", "/entity/counterparty", params={"limit": 1000, "offset": off}, timeout=40)["rows"]
        out += rows
        if len(rows) < 1000 or off > 20000:
            return out
        off += 1000


def _conv_for(d, chat, name):
    """Диалог для клиента: существующий или новый пустой (в списке чатов появится, когда в нём будет сообщение)."""
    r = d.run("SELECT id FROM conv WHERE chat_id=%s", (chat,), one=True)
    if r:
        return r[0]
    return d.run("INSERT INTO conv (chat_id, name, username, status, unread, last_at) VALUES (%s,%s,'','ai',0,0)", (chat, name[:120]), ins=True)


@bp.get("/admin/api/inbox/ms-clients")
@admin.need("inbox")
def ms_clients():
    """Клиенты из МойСклад для добавления в группу: поиск по имени и номеру."""
    q = request.args.get("q", "").strip()[:60]
    try:
        rows = _ms_clients(q) if q else oh.ms("GET", "/entity/counterparty", params={"limit": 100, "order": "updated,desc"}, timeout=20)["rows"]
    except Exception as e:
        return jsonify(ok=False, error="МойСклад не ответил: " + str(e)[:200]), 502
    return jsonify(ok=True, clients=[_cp_card(c) for c in rows])


def _add_clients(d, gid, ids=None):
    """Добавить в группу клиентов МойСклад (ids или всех). Возвращает (добавлено, без контакта)."""
    if ids is None:
        rows = _ms_clients()
    else:
        rows = []
        for i in ids[:500]:
            try:
                rows.append(oh.ms("GET", f"/entity/counterparty/{i}", timeout=15))
            except Exception:
                pass
    convs, nocontact = [], 0
    for cp in rows:
        chat = _cp_chat(cp)
        if not chat:
            nocontact += 1
            continue
        convs.append(_conv_for(d, chat, cp.get("name") or "Клиент"))
    before = len(_members(d, gid))
    _set_members(d, gid, add=convs)
    return len(_members(d, gid)) - before, nocontact


def _members(d, gid):
    return [r[0] for r in d.run("SELECT conv_id FROM grp_member WHERE grp_id=%s", (gid,), many=True) or []]


def _set_members(d, gid, add=(), remove=()):
    have = set(_members(d, gid))
    exist = {r[0] for r in _contacts(d, list(set(add) - have))} if add else set()
    for cid in exist:
        d.run("INSERT INTO grp_member (grp_id, conv_id, at) VALUES (%s,%s,%s)", (gid, cid, time.time()))
    for cid in set(remove) & have:
        d.run("DELETE FROM grp_member WHERE grp_id=%s AND conv_id=%s", (gid, cid))


def _ids(v):
    try:
        return [int(x) for x in (v or [])]
    except (TypeError, ValueError):
        return []


@bp.get("/admin/api/inbox/groups")
@admin.need("inbox")
def groups():
    out = []
    with db() as d:
        for gid, name, at in d.run("SELECT id, name, at FROM grp ORDER BY at DESC", many=True) or []:
            cards = [_card(r) for r in _contacts(d, _members(d, gid))]
            chans = {}
            for c in cards:
                chans[c["channel"]] = chans.get(c["channel"], 0) + 1
            last = d.run("SELECT at, total, sent, skipped, failed, state FROM bcast WHERE grp_id=%s ORDER BY id DESC LIMIT 1", (gid,), one=True)
            out.append({"id": gid, "name": name, "count": len(cards), "open": sum(c["open"] for c in cards), "chans": chans,
                        "last": dict(zip(("at", "total", "sent", "skipped", "failed", "state"), last)) if last else None})
    return jsonify(ok=True, groups=out)


@bp.post("/admin/api/inbox/groups")
@admin.need("inbox")
def group_new():
    b = request.get_json(silent=True) or {}
    name = str(b.get("name", "")).strip()[:80]
    if not name:
        return jsonify(ok=False, error="Назовите группу"), 400
    with db() as d:
        gid = d.run("INSERT INTO grp (name, at) VALUES (%s,%s)", (name, time.time()), ins=True)
        _set_members(d, gid, add=_ids(b.get("members")))
    return jsonify(ok=True, id=gid)


def _bcasts(d, gid):
    rows = d.run("SELECT id, text, file, by_name, at, total, sent, skipped, failed, state FROM bcast WHERE grp_id=%s ORDER BY id DESC LIMIT 30",
                 (gid,), many=True) or []
    return [dict(zip(("id", "text", "file", "by", "at", "total", "sent", "skipped", "failed", "state"), r)) for r in rows]


@bp.get("/admin/api/inbox/groups/<int:gid>")
@admin.need("inbox")
def group_get(gid):
    with db() as d:
        g = d.run("SELECT id, name FROM grp WHERE id=%s", (gid,), one=True)
        if not g:
            return jsonify(ok=False, error="Группа не найдена"), 404
        members = sorted((_card(r) for r in _contacts(d, _members(d, gid))), key=lambda c: (not c["open"], c["name"].lower()))
        return jsonify(ok=True, group={"id": g[0], "name": g[1]}, members=members, broadcasts=_bcasts(d, gid))


@bp.put("/admin/api/inbox/groups/<int:gid>")
@admin.need("inbox")
def group_put(gid):
    b = request.get_json(silent=True) or {}
    with db() as d:
        if not d.run("SELECT id FROM grp WHERE id=%s", (gid,), one=True):
            return jsonify(ok=False, error="Группа не найдена"), 404
        if "name" in b:
            name = str(b.get("name") or "").strip()[:80]
            if not name:
                return jsonify(ok=False, error="Назовите группу"), 400
            d.run("UPDATE grp SET name=%s WHERE id=%s", (name, gid))
        _set_members(d, gid, add=_ids(b.get("add")), remove=_ids(b.get("remove")))
        added = nocontact = 0
        if b.get("all_clients") or b.get("clients"):
            try:
                added, nocontact = _add_clients(d, gid, None if b.get("all_clients") else [str(x) for x in b.get("clients") or []])
            except Exception as e:
                return jsonify(ok=False, error="МойСклад не ответил: " + str(e)[:200]), 502
    return jsonify(ok=True, added=added, nocontact=nocontact)


@bp.delete("/admin/api/inbox/groups/<int:gid>")
@admin.need("inbox")
def group_del(gid):
    with db() as d:
        for bid, in d.run("SELECT id FROM bcast WHERE grp_id=%s", (gid,), many=True) or []:
            d.run("DELETE FROM bcast_item WHERE bcast_id=%s", (bid,))
        d.run("DELETE FROM bcast WHERE grp_id=%s", (gid,))
        d.run("DELETE FROM grp_member WHERE grp_id=%s", (gid,))
        d.run("DELETE FROM grp WHERE id=%s", (gid,))
    return jsonify(ok=True)


def _run(bid, text, f, tpl=None):
    """Отправка рассылки по очереди. Каждое сообщение пишется в диалог клиента (как от менеджера); статус диалога не меняется — ИИ отвечает как обычно."""
    with db() as d:
        items = d.run("SELECT i.conv_id, c.chat_id, c.name, i.state FROM bcast_item i JOIN conv c ON c.id=i.conv_id WHERE i.bcast_id=%s AND i.state IN ('wait','tpl')",
                      (bid,), many=True) or []
    for cid, chat, name, st in items:
        body = text.replace("{имя}", first_name(name) or "").replace("{Имя}", first_name(name) or "")
        body = body.replace(" ,", ",").replace("  ", " ").strip()
        err = ""
        try:
            with db() as d:
                if st == "tpl":                          # WhatsApp, окно закрыто — одобренным шаблоном
                    admin.wa.send_template(chat, tpl["name"], tpl["lang"], [first_name(name) or "клиент"] if tpl["name_param"] else [])
                    inbox.save_msg(d, cid, "manager", f"📋 Шаблон WhatsApp «{tpl['name']}»")
                elif f:
                    admin.deliver_file(d, cid, chat, f["data"], f["name"], f["mime"], "", body)
                else:
                    ext = inbox.send_text(chat, body)
                    inbox.save_msg(d, cid, "manager", body, ext=ext)
        except Exception as e:
            err = str(e)[:300] or "ошибка"
        with db() as d:
            d.run("UPDATE bcast_item SET state=%s, error=%s WHERE bcast_id=%s AND conv_id=%s", ("fail" if err else "sent", err, bid, cid))
            d.run(f"UPDATE bcast SET {'failed=failed+1' if err else 'sent=sent+1'} WHERE id=%s", (bid,))
        time.sleep(PAUSE)
    with db() as d:
        d.run("UPDATE bcast SET state='done' WHERE id=%s", (bid,))


@bp.post("/admin/api/inbox/groups/<int:gid>/send")
@admin.need("inbox")
def group_send(gid):
    """Рассылка группе: text (+ необязательный file). Отправляется в фоне, ход — в истории группы."""
    text = str(request.form.get("text", "")).strip()[:3500]
    up = request.files.get("file")
    f = None
    if up:
        data = up.read()
        if len(data) > admin.MAX_UPLOAD:
            return jsonify(ok=False, error="Файл больше 20 МБ"), 413
        f = {"data": data, "name": (up.filename or "file").replace("/", "_")[:80], "mime": up.mimetype or "application/octet-stream"}
    if not text and not f:
        return jsonify(ok=False, error="Напишите текст или приложите файл"), 400
    tname = str(request.form.get("wa_tpl", "")).strip()[:120]
    tpl = {"name": tname, "lang": str(request.form.get("wa_lang", "ru")).strip()[:10] or "ru",
           "name_param": request.form.get("wa_name") in ("1", "true", "on")} if tname else None
    who = admin.who() or {}
    with db() as d:
        if not d.run("SELECT id FROM grp WHERE id=%s", (gid,), one=True):
            return jsonify(ok=False, error="Группа не найдена"), 404
        cards = [_card(r) for r in _contacts(d, _members(d, gid))]
        if not cards:
            return jsonify(ok=False, error="В группе нет контактов"), 400
        st = {c["id"]: "wait" if c["open"] else "tpl" if tpl and c["channel"] == "wa" else "skip" for c in cards}
        skipped = sum(v == "skip" for v in st.values())
        bid = d.run("INSERT INTO bcast (grp_id, text, file, by_name, at, total, skipped, state) VALUES (%s,%s,%s,%s,%s,%s,%s,'run')",
                    (gid, text, f["name"] if f else None, who.get("name", ""), time.time(), len(cards), skipped), ins=True)
        for c in cards:
            d.run("INSERT INTO bcast_item (bcast_id, conv_id, state, error) VALUES (%s,%s,%s,%s)",
                  (bid, c["id"], st[c["id"]], c["why"] if st[c["id"]] == "skip" else ""))
    threading.Thread(target=_run, args=(bid, text, f, tpl), daemon=True).start()
    return jsonify(ok=True, id=bid, total=len(cards), skipped=skipped)


@bp.get("/admin/api/inbox/broadcasts/<int:bid>")
@admin.need("inbox")
def bcast_get(bid):
    """Отчёт по рассылке: кому не ушло и почему."""
    with db() as d:
        b = d.run("SELECT id, text, file, by_name, at, total, sent, skipped, failed, state FROM bcast WHERE id=%s", (bid,), one=True)
        if not b:
            return jsonify(ok=False, error="Рассылка не найдена"), 404
        rows = d.run("SELECT i.conv_id, c.name, c.chat_id, i.state, i.error FROM bcast_item i JOIN conv c ON c.id=i.conv_id WHERE i.bcast_id=%s",
                     (bid,), many=True) or []
    return jsonify(ok=True, broadcast=dict(zip(("id", "text", "file", "by", "at", "total", "sent", "skipped", "failed", "state"), b)),
                   items=[{"id": r[0], "name": r[1], "channel": inbox.channel(r[2]), "state": r[3], "error": r[4] or ""} for r in rows])

