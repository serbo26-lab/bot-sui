# Установка

Рекомендуемый путь проекта: `/opt/bot-sui`, service user: `bot-sui`, systemd service: `bot-sui.service`.

## Общий порядок

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip jq sqlite3
sudo mkdir -p /opt/bot-sui
sudo chown -R bot-sui:bot-sui /opt/bot-sui
cd /opt/bot-sui
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
chmod 600 config.json
```

После настройки systemd:

```bash
sudo systemctl daemon-reload
sudo systemctl enable bot-sui
sudo systemctl start bot-sui
sudo journalctl -u bot-sui -n 100 --no-pager
```
