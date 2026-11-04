#!/usr/bin/env python3
"""Копия базы Render → своя база сервера (этап db). Запускает deploy/apply.sh, когда в deploy/db-copy меняется отметка.

Только чтение базы Render: pg_dump и подсчёт строк — в транзакции read-only. Пароль Render не попадает ни в командную строку,
ни в журнал, ни в отчёт: подключение — через переменные PG* процесса, в выводе ошибок адрес заменяется на «база Render».
Итог — одним сообщением владельцу «🖥»: таблицы, строки, расхождения (база Render живая — пара строк в свежих таблицах
может разойтись за время копирования)."""
import json
import os
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlparse

ENV = "/etc/amura/app.env"
LOCAL = "/etc/amura/local.env"
BACKUPS = "/var/backups/amura"


def load(path):
    out = {}
    with open(path) as f:
        for line in f:
            if "=" in line:
                k, v = line.rstrip("\n").split("=", 1)
                out[k] = json.loads(v) if v.startswith('"') else v
    return out


def pg_env(url, external=False):
    u = urlparse(url)
    host = u.hostname or ""
    if external and "." not in host:                     # внутренний адрес Render → внешний (Франкфурт)
        host += ".frankfurt-postgres.render.com"
    return {"PGHOST": host, "PGPORT": str(u.port or 5432), "PGUSER": unquote(u.username or ""), "PGPASSWORD": unquote(u.password or ""),
            "PGDATABASE": u.path.lstrip("/"), "PGSSLMODE": "require" if external else "disable", "PGCONNECT_TIMEOUT": "20"}


def run(args, env, secret, **kw):
    r = subprocess.run(args, env={**os.environ, **env}, capture_output=True, text=True, **kw)
    if r.returncode:
        err = (r.stderr or r.stdout or "").replace(env.get("PGPASSWORD") or "\0", "***")
        if secret:
            err = err.replace(secret, "база Render")
        raise RuntimeError(f"{args[0]}: {err.strip()[-400:]}")
    return r.stdout


COUNT = ("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1")


def counts(env, secret):
    tables = [t for t in run(["psql", "-tAc", COUNT], env, secret).split() if t]
    sql = " UNION ALL ".join(f"SELECT '{t}', count(*) FROM public.\"{t}\"" for t in tables) or "SELECT '', 0"
    out = run(["psql", "-tA", "-F", "\t", "-c", sql], env, secret)
    return {a: int(b) for a, b in (x.split("\t") for x in out.splitlines() if "\t" in x)}


def main():
    app = load(ENV)
    src = pg_env(app["RENDER_DATABASE_URL"], external=True)
    src["PGOPTIONS"] = "-c default_transaction_read_only=on"     # Render — только чтение
    dst = pg_env(load(LOCAL)["DATABASE_URL"])
    secret = src["PGHOST"]
    t0 = time.time()
    with tempfile.TemporaryDirectory(dir=BACKUPS) as d:
        dump = os.path.join(d, "render.dump")
        run(["pg_dump", "-Fc", "--no-owner", "--no-acl", "-f", dump], src, secret, timeout=3600)
        size = os.path.getsize(dump)
        subprocess.run(["systemctl", "stop", "amura-app"], check=False)
        try:
            run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl", "-d", dst["PGDATABASE"], dump], dst, secret,
                timeout=3600)
        finally:
            subprocess.run(["systemctl", "start", "amura-app"], check=False)
    a, b = counts(src, secret), counts(dst, secret)
    diff = [f"{t}: Render {a.get(t, '—')}, сервер {b.get(t, '—')}" for t in sorted(set(a) | set(b)) if a.get(t) != b.get(t)]
    text = (f"копия базы Render → сервер за {round(time.time() - t0)} с, дамп {size / 1e6:.1f} МБ: таблиц {len(a)}, "
            f"строк {sum(a.values())} → {sum(b.values())}; " + ("расхождений нет" if not diff else
            f"расхождений {len(diff)} (база Render живая, свежие строки за время копирования): " + "; ".join(diff[:10])))
    print(text)
    return text


if __name__ == "__main__":
    try:
        msg = main()
        level = "ok"
    except Exception as e:
        msg, level = f"копия базы не удалась: {str(e)[:600]}", "alert"
        print(msg, file=sys.stderr)
    subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "report.py"), "send", level, msg])
    sys.exit(0 if level == "ok" else 1)
