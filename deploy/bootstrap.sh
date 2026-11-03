#!/bin/bash
# Первичная настройка сервера AMURA (Hoster.kz, Ubuntu 24.04). Запускается один раз от root в веб-консоли Hoster:
#   curl -fsSL https://raw.githubusercontent.com/Mussarov402/amura-shop/main/deploy/bootstrap.sh | bash
# Секретов не содержит и не просит. Дальше сервер сам раз в минуту забирает изменения с GitHub (deploy/pull.sh)
# и сообщает итог системе AMURA (подписью ключа сервера). Повторный запуск безопасен.
set -euo pipefail
REPO="https://github.com/Mussarov402/amura-shop.git"
APP=/opt/amura/app
[ "$(id -u)" = 0 ] || { echo "Нужно запускать от root"; exit 1; }
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a NEEDRESTART_SUSPEND=1
echo "== 1/5 Обновление системы =="
apt-get update -y
apt-get -y -o Dpkg::Options::=--force-confold upgrade
apt-get install -y git curl ufw fail2ban unattended-upgrades nginx certbot python3-certbot-nginx \
  postgresql postgresql-contrib python3-venv python3-pip python3-cryptography jq
echo "== 2/5 Защита: firewall и обновления безопасности =="
ufw allow OpenSSH >/dev/null; ufw allow 'Nginx Full' >/dev/null; ufw --force enable >/dev/null
echo 'APT::Periodic::Update-Package-Lists "1"; APT::Periodic::Unattended-Upgrade "1";' > /etc/apt/apt.conf.d/20auto-upgrades
systemctl enable --now fail2ban >/dev/null 2>&1 || true
echo "== 3/5 Код системы AMURA с GitHub (только чтение) =="
id amura >/dev/null 2>&1 || useradd -m -s /bin/bash amura
mkdir -p /opt/amura /etc/amura /var/lib/amura /var/backups/amura
chmod 700 /etc/amura /var/backups/amura
if [ -d "$APP/.git" ]; then git -C "$APP" fetch -q origin main && git -C "$APP" reset -q --hard origin/main
else git clone -q --depth 50 "$REPO" "$APP"; fi
echo "== 4/5 Ключ сервера (секретная часть остаётся только на сервере) =="
python3 "$APP/deploy/report.py" keygen
install -m 644 "$APP/deploy/amura-pull.service" /etc/systemd/system/amura-pull.service
install -m 644 "$APP/deploy/amura-pull.timer" /etc/systemd/system/amura-pull.timer
systemctl daemon-reload
systemctl enable --now amura-pull.timer >/dev/null
echo "== 5/5 Первый шаг настройки и связь с системой AMURA =="
bash "$APP/deploy/apply.sh" || true
python3 "$APP/deploy/report.py" hello || echo "Связь с системой AMURA не удалась — повторится автоматически"
echo
echo "ГОТОВО. Отпечаток ключа сервера: $(python3 "$APP/deploy/report.py" fpr)"
echo "Больше ничего вводить не нужно — консоль можно закрыть."
