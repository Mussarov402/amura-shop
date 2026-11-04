#!/bin/bash
# amura-dbcopy.timer: копия базы Render, если в deploy/db-copy новая отметка; после неудачи — не чаще раза в 30 минут.
APP=/opt/amura/app
[ -f /var/lib/amura/live ] && exit 0                 # рабочий режим: своя база — главная, копий с Render больше нет
WANT=$(cat "$APP/deploy/db-copy" 2>/dev/null || true)
[ -n "$WANT" ] || exit 0
[ "$WANT" = "$(cat /var/lib/amura/db-copy-done 2>/dev/null || true)" ] && exit 0
TRY=/var/lib/amura/db-copy-try
[ -f "$TRY" ] && [ -z "$(find "$TRY" -mmin +30)" ] && exit 0
touch "$TRY"
python3 "$APP/deploy/dbcopy.py" && echo "$WANT" > /var/lib/amura/db-copy-done && rm -f "$TRY"
