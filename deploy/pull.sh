#!/bin/bash
# Раз в минуту (amura-pull.timer): есть изменения на GitHub — забираем и применяем deploy/apply.sh, итог — в систему AMURA.
set -uo pipefail
APP=/opt/amura/app
cd "$APP" || exit 1
git fetch -q origin main || exit 0
NEW=$(git rev-parse origin/main); OLD=$(git rev-parse HEAD)
[ "$NEW" = "$OLD" ] && [ ! -f /var/lib/amura/retry ] && exit 0
# только данные сайтов (выгрузка каталога, фото, страницы с GitHub Pages, документы) — приложение их с диска не читает:
# забираем без apply.sh, чтобы не перезапускать систему AMURA несколько раз в день из-за выгрузки каталога
if [ ! -f /var/lib/amura/retry ] && ! git diff --name-only "$OLD" "$NEW" | grep -qvE '^(catalog\.json|prices\.bin|img/|shop/|retail/|opt/|docs/|cards/|motion/|index\.html$|[^/]*\.md$)'; then
  git reset -q --hard origin/main
  exit 0
fi
git reset -q --hard origin/main
python3 deploy/report.py hello >/dev/null 2>&1 || true        # пока ключ сервера не закреплён в репозитории
if OUT=$(bash deploy/apply.sh 2>&1); then
  rm -f /var/lib/amura/retry
  python3 deploy/report.py send ok "применено ${NEW:0:7}: $(echo "$OUT" | tail -n 3 | tr '\n' ' ')"
else
  touch /var/lib/amura/retry
  if echo "$OUT" | grep -qE "HTTP Error (404|5[0-9][0-9])"; then      # система AMURA ещё без нужной части — ждём деплой Render
    python3 deploy/report.py send info "⏳ жду деплой Render (${NEW:0:7}): $(echo "$OUT" | grep -oE 'HTTP Error [0-9]+[^ ]*' | tail -n 1)"
  else
    python3 deploy/report.py send alert "ошибка применения ${NEW:0:7}: $(echo "$OUT" | tail -n 8 | tr '\n' ' ')"
  fi
fi
