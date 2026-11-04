#!/bin/bash
# nginx сервера AMURA: все домены → приложение (127.0.0.1:8000). Вызывается из apply.sh и certs.sh, идемпотентно.
# http (80): проверка Let's Encrypt из /var/www/acme; домен с сертификатом — переадресация на https, без — сразу приложение
# (до выпуска сертификата сайты отвечают по http). https (443) — только для доменов, у которых сертификат уже есть.
# Конфиг целиком наш (certbot certonly --webroot не правит nginx) — перезапись не теряет https.
set -euo pipefail
DOMAINS="${AMURA_DOMAINS:-amura.kz www.amura.kz opt.amura.kz admin.amura.kz api.amura.kz server.amura.kz}"
LE=/etc/letsencrypt/live
OUT=/etc/nginx/sites-available/amura
mkdir -p /var/www/acme/.well-known/acme-challenge

proxy() {
  cat <<'EOP'
    client_max_body_size 50m;
    add_header X-Amura-Server hoster always;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 130s;
    }
EOP
}

{
  echo "# создано deploy/nginx.sh — вручную не править"
  echo "server {"
  echo "    listen 80 default_server;"
  echo "    server_name _;"
  echo "    location /.well-known/acme-challenge/ { root /var/www/acme; default_type text/plain; }"
  proxy
  echo "}"
  for D in $DOMAINS; do
    [ -s "$LE/$D/fullchain.pem" ] || continue
    cat <<EOS
server {
    listen 80;
    server_name $D;
    location /.well-known/acme-challenge/ { root /var/www/acme; default_type text/plain; }
    location / { return 301 https://\$host\$request_uri; }
}
server {
    listen 443 ssl http2;
    server_name $D;
    ssl_certificate $LE/$D/fullchain.pem;
    ssl_certificate_key $LE/$D/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:amura:10m;
EOS
    proxy
    echo "}"
  done
} > "$OUT.new"

if ! cmp -s "$OUT.new" "$OUT"; then
  cp -f "$OUT" "$OUT.old" 2>/dev/null || true
  mv "$OUT.new" "$OUT"
  ln -sf "$OUT" /etc/nginx/sites-enabled/amura
  rm -f /etc/nginx/sites-enabled/default
  if nginx -t -q 2>/dev/null; then
    systemctl reload nginx
  else                                            # ошибка конфига — возвращаем прежний, nginx продолжает работать
    [ -f "$OUT.old" ] && mv "$OUT.old" "$OUT"
    nginx -t
    exit 1
  fi
else
  rm -f "$OUT.new"
fi
echo "nginx: https у $(for D in $DOMAINS; do [ -s "$LE/$D/fullchain.pem" ] && printf '%s ' "$D"; done)"
