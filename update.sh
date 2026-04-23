#!/usr/bin/env bash
set -e

PROJECT_DIR="/root/bot-sui"
SERVICE_NAME="bot-sui"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root"
  exit 1
fi

cd "$PROJECT_DIR"

git pull --ff-only

if [ ! -d venv ]; then
  python3 -m venv venv
fi

source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m py_compile bot.py

chmod +x run.sh backup.sh healthcheck.sh

cp systemd/${SERVICE_NAME}.service /etc/systemd/system/${SERVICE_NAME}.service
cp systemd/${SERVICE_NAME}-backup.service /etc/systemd/system/${SERVICE_NAME}-backup.service
cp systemd/${SERVICE_NAME}-backup.timer /etc/systemd/system/${SERVICE_NAME}-backup.timer

systemctl daemon-reload
systemctl restart ${SERVICE_NAME}

echo
echo "Updated Bot S-UI"
echo "Status:"
echo "  systemctl status ${SERVICE_NAME} --no-pager"
