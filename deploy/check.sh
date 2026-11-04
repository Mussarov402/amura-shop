#!/bin/bash
# Проверка доменов AMURA снаружи (по DNS, как у покупателя): куда смотрит домен и отвечает ли https.
# local — то же, но на этом сервере через nginx (127.0.0.1) с нужным именем: готов ли сервер принять домен до смены DNS.
# Ответ сервера помечен заголовком X-Amura-Server (nginx), так видно, кто ответил — этот сервер или Render.
DOMAINS="${AMURA_DOMAINS:-amura.kz www.amura.kz opt.amura.kz admin.amura.kz api.amura.kz server.amura.kz}"
ME="${AMURA_SERVER_IP:-89.126.206.3}"
OUT=""
for D in $DOMAINS; do
  if [ "${1:-}" = local ]; then
    if [ -s "/etc/letsencrypt/live/$D/fullchain.pem" ]; then
      C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 --resolve "$D:443:127.0.0.1" "https://$D/" || true)
      OUT="$OUT $D https $C;"
    else
      C=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 -H "Host: $D" "http://127.0.0.1/" || true)
      OUT="$OUT $D http $C;"
    fi
    continue
  fi
  IP=$(getent ahostsv4 "$D" | awk 'NR==1{print $1}')
  V6=$(getent ahostsv6 "$D" | awk '$1 !~ /^::ffff:/ {print $1; exit}')
  H=$(curl -sI --max-time 20 "https://$D/" 2>/dev/null)
  C=$(echo "$H" | awk 'NR==1{print $2}')
  WHO=$(echo "$H" | grep -qi '^x-amura-server:' && echo "сервер" || { [ "$IP" = "$ME" ] && echo "сервер?" || echo "Render/старый"; })
  OUT="$OUT $D → ${IP:-нет A} ($WHO) https ${C:-нет ответа}${V6:+, есть AAAA $V6 — удалить};"
done
echo "${OUT# }"
