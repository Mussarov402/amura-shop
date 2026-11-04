#!/bin/bash
# Применение настройки сервера — идемпотентно, запускается при каждом изменении на GitHub.
# deploy/stage: base — только система; app — приложение в режиме ожидания (AMURA_STANDBY=1: фон выключен, не пишет
# в МойСклад, площадки и Telegram), своя база Postgres, nginx; db — плюс копия базы Render (только чтение) по отметке
# deploy/db-copy; live — рабочий режим: один раз финальная копия базы Render (Render в это время «только чтение»,
# AMURA_READONLY=1) со сверкой строк без расхождений, затем app:app с фоном; копии базы больше не делаются никогда.
# Откат: deploy/stage обратно на db (PR) — приложение снова в ожидании, своя база не перезаписывается.
set -euo pipefail
APP=/opt/amura/app
VENV=/opt/amura/venv
STAGE=$(cat "$APP/deploy/stage" 2>/dev/null || echo base)
LIVE=/var/lib/amura/live                     # отметка: переход в рабочий режим сделан (финальная копия принята)
mkdir -p /var/lib/amura
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

# переменные приложения: с Render, зашифрованы ключом этого сервера (раз в сутки обновляем; в рабочем режиме — нет:
# api.amura.kz тогда смотрит на этот же сервер; последний раз — при переходе, см. ниже)
ENVF=/etc/amura/app.env
if [ ! -f "$ENVF" ] || { [ "$STAGE" != live ] && [ -n "$(find "$ENVF" -mmin +1440)" ]; } || { [ "$STAGE" = live ] && [ ! -f "$LIVE" ]; }; then
  python3 "$APP/deploy/report.py" env "$ENVF"
fi
LOCAL=/etc/amura/local.env
umask 077
cat > "$LOCAL" <<EOV
DATABASE_URL="postgresql://amura:$PASS@127.0.0.1:5432/amura"
AMURA_STANDBY="$([ "$STAGE" = live ] && echo 0 || echo 1)"
AMURA_READONLY="0"
PORT="8000"
EOV
[ "$STAGE" = live ] && echo 'PUBLIC_URL="https://api.amura.kz"' >> "$LOCAL"
chown amura: "$ENVF" "$LOCAL"

# --- откат (stage снова не live): отметку перехода убираем — при следующем переходе снова финальная копия с Render
#     (перед ней — резервная копия своей базы, данные рабочего режима не теряются) ---
if [ "$STAGE" != live ] && [ -f "$LIVE" ]; then
  mv "$LIVE" "$LIVE.rollback-$(date +%s)"
  echo "откат: сервер снова в ожидании"
fi

# --- переход в рабочий режим (один раз): фоновые копии выключить, финальная копия и сверка строк — без расхождений.
#     Не получилось — выходим с ошибкой (тревога владельцу), приложение остаётся в ожидании; pull.sh повторит. ---
if [ "$STAGE" = live ] && [ ! -f "$LIVE" ]; then
  systemctl disable --now amura-dbcopy.timer >/dev/null 2>&1 || true
  systemctl stop amura-dbcopy.service >/dev/null 2>&1 || true
  command -v pg_dump >/dev/null || apt-get install -y -q postgresql-client >/dev/null
  python3 "$APP/deploy/dbcopy.py" --final
  date -u '+%F %T UTC' > "$LIVE"
  echo "финальная копия принята, рабочий режим"
fi

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

# nginx: все домены AMURA → приложение (deploy/nginx.sh); сертификаты — таймер amura-certs (deploy/certs.sh),
# выпускаются и до смены DNS: проверку Let's Encrypt Render пересылает сюда
bash "$APP/deploy/nginx.sh"
install -m 644 "$APP/deploy/amura-certs.service" /etc/systemd/system/amura-certs.service
install -m 644 "$APP/deploy/amura-certs.timer" /etc/systemd/system/amura-certs.timer
systemctl daemon-reload
systemctl enable --now amura-certs.timer >/dev/null 2>&1

# проверка
for i in $(seq 1 30); do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ || true)
  [ "$CODE" = 200 ] && break; sleep 2
done
[ "$CODE" = 200 ] || { echo "приложение: HTTP $CODE"; exit 1; }

# --- копия базы Render (этап db и дальше): таймер раз в 10 минут, копия — когда в deploy/db-copy новая отметка
#     (меняется PR-ом, когда нужна свежая копия); неудачная попытка повторяется не чаще раза в 30 минут ---
case "$STAGE" in db)
  command -v pg_dump >/dev/null || apt-get install -y -q postgresql-client >/dev/null
  install -m 644 "$APP/deploy/amura-dbcopy.service" /etc/systemd/system/amura-dbcopy.service
  install -m 644 "$APP/deploy/amura-dbcopy.timer" /etc/systemd/system/amura-dbcopy.timer
  systemctl daemon-reload
  systemctl enable --now amura-dbcopy.timer >/dev/null 2>&1
  echo "копия базы: отметка $(cat "$APP/deploy/db-copy" 2>/dev/null || echo —), сделана $(cat /var/lib/amura/db-copy-done 2>/dev/null || echo —)"
;; esac

# итог последней строкой (её pull.sh пересылает владельцу): режим и ответ каждого домена через nginx этого сервера
echo "приложение: HTTP $CODE ($([ "$STAGE" = live ] && echo "рабочий режим с $(cat "$LIVE")" || echo ожидание, фон выключен)); домены здесь: $(bash "$APP/deploy/check.sh" local)"
