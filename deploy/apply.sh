#!/bin/bash
# Применение настройки сервера — идемпотентно, запускается при каждом изменении на GitHub.
# Этап задаёт deploy/stage: base — только система; app/live — следующие PR (приложение, база, сайты).
set -euo pipefail
APP=/opt/amura/app
STAGE=$(cat "$APP/deploy/stage" 2>/dev/null || echo base)
echo "этап: $STAGE"
systemctl is-active --quiet postgresql || systemctl start postgresql
systemctl is-active --quiet nginx || systemctl start nginx
echo "диск: $(df -h / | awk 'NR==2{print $4" свободно из "$2}'), память: $(free -m | awk '/Mem/{print $7" МБ доступно из "$2}')"
echo "postgres: $(sudo -u postgres psql -tAc 'select version()' | cut -d' ' -f1-2)"
