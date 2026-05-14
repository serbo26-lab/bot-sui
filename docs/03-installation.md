# Установка

Этот раздел описывает общий порядок установки. Конкретный install script может отличаться в вашей версии проекта.

## 1. Подготовить сервер

Рекомендуемый путь проекта: `/opt/bot-sui`. Сервисный пользователь: `bot-sui`. Systemd service: `bot-sui.service`.

## 2. Установить зависимости

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip jq sqlite3
```

## 3. Подготовить Python environment

```bash
cd /opt/bot-sui
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 4. Настроить config.json

```bash
cp config.example.json config.json
nano config.json
chmod 600 config.json
```

## 5. Настроить systemd

Пример service:

```ini
[Unit]
Description=Bot S-UI Telegram service
After=network-online.target
Wants=network-online.target

[Service]
User=bot-sui
Group=bot-sui
WorkingDirectory=/opt/bot-sui
ExecStart=/opt/bot-sui/venv/bin/python /opt/bot-sui/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable bot-sui
sudo systemctl start bot-sui
```

## 6. Проверить запуск

```bash
sudo systemctl status bot-sui --no-pager
sudo journalctl -u bot-sui -n 100 --no-pager
```

После запуска проверьте `/start`, админ-панель, S-UI API, production doctor и тестовый сценарий создания подписки.
