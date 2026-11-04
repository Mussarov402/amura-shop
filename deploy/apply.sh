#!/bin/bash
# Применение настройки сервера — идемпотентно, запускается при каждом изменении на GitHub.
# deploy/stage: base — только система; app — приложение в режиме ожидания (AMURA_STANDBY=1: фон выключен, не пишет
# в МойСклад, площадки и Telegram), своя база Postgres, nginx; db/live — следующие PR (копия базы, переключение).
set -euo pipefail
APP=/opt/amura/app
VENV=/opt/amura/venv
STAGE=$(cat "$APP/deploy/stage" 2>/dev/null || echo base)
echo "этап: $STAGE"
systemctl is-active --quiet postgresql || systemctl start postgresql
systemctl is-active --quiet nginx || systemctl start nginx
echo "диск: $(df -h / | awk 'NR==2{print $4" свободно из "$2}'), память: $(free -m | awk '/Mem/{print $7" МБ доступно из "$2}')"
[ "$STAGE" = base ] && exit 0

# --- приложение ---
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip >/dev/null
"$VENV/bin/pip" install -q -r "$APP/requirements.txt"
mkdir -p "$APP/fonts" && cp -n "$APP"/DejaVuSans*.ttf "$APP/fonts/" 2>/dev/null || true

# своя база: пароль генерируется на сервере и не покидает его
if [ ! -f /etc/amura/db.pass ]; then
  umask 077; head -c 24 /dev/urandom | base64 | tr -d '/+=' > /etc/amura/db.pass
fi
PASS=$(cat /etc/amura/db.pass)
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='amura'" | grep -q 1 || \
  sudo -u postgres psql -qc "CREATE ROLE amura LOGIN PASSWORD '$PASS'"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='amura'" | grep -q 1 || \
  sudo -u postgres createdb -O amura amura

# переменные приложения: с Render, зашифрованы ключом этого сервера (раз в сутки обновляем)
ENVF=/etc/amura/app.env
if [ ! -f "$ENVF" ] || [ -n "$(find "$ENVF" -mmin +1440)" ]; then
  python3 "$APP/deploy/report.py" env "$ENVF"
fi
LOCAL=/etc/amura/local.env
umask 077
cat > "$LOCAL" <<EOV
DATABASE_URL="postgresql://amura:$PASS@127.0.0.1:5432/amura"
AMURA_STANDBY="$([ "$STAGE" = live ] && echo 0 || echo 1)"
PORT="8000"
EOV
chown amura: "$ENVF" "$LOCAL"

cat > /etc/systemd/system/amura-app.service <<EOU
[Unit]
Description=AMURA: приложение (gunicorn)
After=network-online.target postgresql.service

[Service]
User=amura
WorkingDirectory=$APP
EnvironmentFile=$ENVF
EnvironmentFile=$LOCAL
ExecStart=$VENV/bin/gunicorn $([ "$STAGE" = live ] && echo app:app || echo app_standby:app) --bind 127.0.0.1:8000 --workers 1 --threads 8 --timeout 120
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOU
systemctl daemon-reload
systemctl enable amura-app >/dev/null 2>&1
systemctl restart amura-app

# nginx: server.amura.kz → приложение (сертификат — когда DNS server.amura.kz указывает сюда)
cat > /etc/nginx/sites-available/amura <<'EON'
server {
    listen 80;
    server_name server.amura.kz;
    client_max_body_size 50m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 130s;
    }
}
EON
ln -sf /etc/nginx/sites-available/amura /etc/nginx/sites-enabled/amura
rm -f /etc/nginx/sites-enabled/default
nginx -t -q && systemctl reload nginx
if [ ! -d /etc/letsencrypt/live/server.amura.kz ]; then
  certbot --nginx -n --agree-tos --register-unsafely-without-email -d server.amura.kz --redirect >/dev/null 2>&1 \
    && echo "сертификат server.amura.kz выпущен" || echo "сертификат пока не выпущен (DNS server.amura.kz?)"
fi

# проверка
for i in $(seq 1 30); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ || true)
  [ "$CODE" = 200 ] && break; sleep 2
done
echo "приложение: HTTP $CODE ($([ "$STAGE" = live ] && echo работает || echo ожидание, фон выключен))"
[ "$CODE" = 200 ]
