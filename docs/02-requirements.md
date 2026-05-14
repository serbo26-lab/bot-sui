# Требования

Для запуска нужны VPS с Ubuntu, Python, systemd, SQLite, домен, установленная S-UI/sing-box панель, Telegram bot token и TGID администратора.

## S-UI и sing-box

Перед настройкой бота должен быть подготовлен основной сервер с S-UI/sing-box: API, subscription URL, inbounds и сертификаты. Подробная установка S-UI зависит от версии панели и не является основной частью репозитория.

## Домен и сертификаты

Рекомендуемая схема: example.com, panel.example.com, sub.example.com, node1.example.com. Обычно используется wildcard-сертификат *.example.com.

## Remote nodes

Для remote nodes нужны VPS, SSH-доступ от main-сервера, DNS-записи, открытые порты и SSH key.
