# Требования

## Базовые требования

Для запуска проекта рекомендуется VPS с Ubuntu, Python 3.12 или совместимая версия, systemd, SQLite, доступ к shell, домен, S-UI/sing-box и Telegram bot token.

## S-UI и sing-box

Перед настройкой бота должен быть подготовлен основной сервер с S-UI/sing-box.

Минимально нужно: работающая S-UI панель, доступ к S-UI API, subscription URL, настроенные inbounds, wildcard-сертификат и понимание, какие inbounds будут использоваться по умолчанию.

Подробная установка S-UI зависит от выбранной версии панели и не является основной частью этого репозитория.

## Домен и сертификаты

Рекомендуемая схема:

```text
example.com
panel.example.com
sub.example.com
node1.example.com
node2.example.com
```

Сертификат обычно wildcard: `*.example.com` и `example.com`.

## Telegram

Нужно создать бота через BotFather и получить token. Также нужен Telegram ID администратора.

## Remote nodes

Для remote nodes нужны отдельные VPS, SSH-доступ от main-сервера, DNS-записи, открытые порты и SSH private key на main.
