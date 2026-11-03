#!/bin/bash
# Раз в минуту (amura-pull.timer): есть изменения на GitHub — забираем и применяем deploy/apply.sh, итог — в систему AMURA.
set -uo pipefail
APP=/opt/amura/app
cd "$APP" || exit 1
git fetch -q origin main || exit 0
NEW=$(git rev-parse origin/main); OLD=$(git rev-parse HEAD)
[ "$NEW" = "$OLD" ] && [ ! -f /var/lib/amura/retry ] && exit 0
git reset -q --hard origin/main
python3 deploy/report.py hello >/dev/null 2>&1 || true        # пока ключ сервера не закреплён в репозитории
if OUT=$(bash deploy/apply.sh 2>&1); then
  rm -f /var/lib/amura/retry
  python3 deploy/report.py send ok "применено ${NEW:0:7}: $(echo "$OUT" | tail -n 3 | tr '\n' ' ')"
else
  touch /var/lib/amura/retry
  python3 deploy/report.py send alert "ошибка применения ${NEW:0:7}: $(echo "$OUT" | tail -n 8 | tr '\n' ' ')"
fi
