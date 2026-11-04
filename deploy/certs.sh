#!/bin/bash
# amura-certs.timer (каждые 2 минуты): сертификаты Let's Encrypt для доменов AMURA, у которых их ещё нет.
# Проверка — HTTP-01 через /var/www/acme. До смены DNS запрос Let's Encrypt приходит на Render, и тот пересылает его
# сюда (ops.acme) — сертификаты выпускаются заранее; после смены DNS — сразу сюда.
# Чтобы не тратить лимит неудачных проверок Let's Encrypt (5 в час на домен), сначала свой пробный файл:
# certbot запускаем только для домена, где пробный файл виден снаружи по http://домен/.well-known/acme-challenge/…
set -uo pipefail
DOMAINS="${AMURA_DOMAINS:-amura.kz www.amura.kz opt.amura.kz admin.amura.kz api.amura.kz server.amura.kz}"
APP=/opt/amura/app
WEB=/var/www/acme
STATE=/var/lib/amura
LE=/etc/letsencrypt/live
mkdir -p "$WEB/.well-known/acme-challenge" "$STATE"
NEW=""; WAIT=""

for D in $DOMAINS; do
  [ -s "$LE/$D/fullchain.pem" ] && continue
  T="amura-probe-$(head -c 9 /dev/urandom | base64 | tr -d '/+=')"
  echo "$T" > "$WEB/.well-known/acme-challenge/$T"
  GOT=$(curl -sL --max-time 15 "http://$D/.well-known/acme-challenge/$T" 2>/dev/null | head -c 200)
  rm -f "$WEB/.well-known/acme-challenge/$T"
  if [ "$GOT" != "$T" ]; then
    WAIT="$WAIT $D"
    continue
  fi
  if certbot certonly --webroot -w "$WEB" -n --agree-tos --register-unsafely-without-email --cert-name "$D" -d "$D" \
       --deploy-hook "systemctl reload nginx" >/dev/null 2>&1; then
    NEW="$NEW $D"
  else
    WAIT="$WAIT $D(certbot)"
  fi
done

if [ -n "$NEW" ]; then
  bash "$APP/deploy/nginx.sh" >/dev/null
  python3 "$APP/deploy/report.py" send ok "сертификаты выпущены:$NEW${WAIT:+; ждут:$WAIT}"
fi
# журнал, что ещё ждёт (одной строкой, только при изменении — без спама в журнал)
LAST=$(cat "$STATE/certs-wait" 2>/dev/null || true)
[ "$WAIT" != "$LAST" ] && { echo "$WAIT" > "$STATE/certs-wait"; echo "сертификаты ждут:${WAIT:- —}"; }
# после перехода в рабочий режим: сводка доменов снаружи — владельцу при каждом изменении (смена DNS по доменам)
if [ -f "$STATE/live" ]; then
  NOW=$(bash "$APP/deploy/check.sh")
  if [ "$NOW" != "$(cat "$STATE/check-last" 2>/dev/null)" ]; then
    echo "$NOW" > "$STATE/check-last"
    python3 "$APP/deploy/report.py" send ok "домены: $NOW"
  fi
fi
exit 0
