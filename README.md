# Bot S-UI

Telegram-бот для S-UI / sing-box.

Основные возможности:
- покупка подписок через Telegram Stars
- автоматическая выдача подписки после оплаты
- продление действующих подписок
- раздел «Мои подписки»
- раздел «Мои платежи»
- тестовый доступ
- FAQ
- поддержка с тикетами
- antiabuse-контроль подключений
- напоминания об окончании подписки
- Telegram proxy для активных клиентов
- админ-панель для управления ботом

## Установка

Установка из GitHub:

```bash
cd /root
git clone https://github.com/serbo26-lab/bot-sui.git
cd bot-sui
bash install.sh
````

После установки сервис запускается через systemd.

Проверка статуса:

```bash
systemctl status bot-sui --no-pager
journalctl -u bot-sui -n 100 --no-pager
```

## Обновление

Обновление проекта из репозитория:

```bash
cd /root/bot-sui
bash update.sh
```

## Ручной запуск

Если нужно проверить запуск вручную:

```bash
cd /root/bot-sui
source venv/bin/activate
python -m py_compile bot.py
bash run.sh
```

## Бэкапы

В проекте предусмотрен backup-механизм.

Ручной запуск бэкапа:

```bash
cd /root/bot-sui
bash backup.sh
```

Если нужно включить внешний timer systemd:

```bash
systemctl enable --now bot-sui-backup.timer
systemctl status bot-sui-backup.timer --no-pager
```

## Структура проекта

```text
bot-sui/
├── bot.py
├── requirements.txt
├── run.sh
├── backup.sh
├── healthcheck.sh
├── install.sh
├── update.sh
└── systemd/
    ├── bot-sui.service
    ├── bot-sui-backup.service
    └── bot-sui-backup.timer
```

## Настройка

Перед запуском необходимо проверить и при необходимости изменить параметры в `bot.py`:

* `BOT_TOKEN`
* `ADMIN_IDS`
* `SUI_API_URL`
* `SUI_TOKEN`
* `SUI_SUB_URL`
* `SUI_SERVER_NAME`
* `SUI_SERVER_CODE`
* `SUI_DEFAULT_INBOUNDS`
Так-же изменить под себя текста, описание, приветствие.

## Systemd

Основной сервис:

```bash
systemctl enable --now bot-sui
```

Перезапуск:

```bash
systemctl restart bot-sui
```

Остановка:

```bash
systemctl stop bot-sui
```

## Примечания

* проект рассчитан на установку в `/root/bot-sui`
* данные SQLite, логи, бэкапы и виртуальное окружение не должны коммититься в репозиторий
* перед публичным использованием рекомендуется вынести токены и чувствительные параметры из `bot.py` в отдельный конфиг или переменные окружения

## Лицензия

Частный проект / personal use.

```
`Telegram bot for S-UI`
```

::contentReference[oaicite:1]{index=1}

[1]: https://github.com/serbo26-lab/bot-sui "GitHub - serbo26-lab/bot-sui · GitHub"
