#!/usr/bin/env bash
set -e

PROJECT_DIR="/root/bot-sui"
SERVICE_NAME="bot-sui"
BACKUP_SERVICE_NAME="bot-sui-backup"
BACKUP_TIMER_NAME="bot-sui-backup"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root"
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer expects Debian/Ubuntu with apt-get"
  exit 1
fi

cd "$PROJECT_DIR"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip sqlite3 tar

if [ ! -d venv ]; then
  python3 -m venv venv
fi

source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

chmod +x run.sh backup.sh healthcheck.sh

install -d /etc/systemd/system
cp systemd/${SERVICE_NAME}.service /etc/systemd/system/${SERVICE_NAME}.service
cp systemd/${BACKUP_SERVICE_NAME}.service /etc/systemd/system/${BACKUP_SERVICE_NAME}.service
cp systemd/${BACKUP_TIMER_NAME}.timer /etc/systemd/system/${BACKUP_TIMER_NAME}.timer

systemctl daemon-reload
systemctl enable --now ${SERVICE_NAME}

echo
echo "Installed Bot S-UI into ${PROJECT_DIR}"
echo
echo "Status:"
echo "  systemctl status ${SERVICE_NAME} --no-pager"
echo
echo "Logs:"
echo "  journalctl -u ${SERVICE_NAME} -n 100 --no-pager"
echo
echo "Optional external backup timer:"
echo "  systemctl enable --now ${BACKUP_TIMER_NAME}.timer"
echo
echo "Manual backup:"
echo "  cd ${PROJECT_DIR} && bash backup.sh"
