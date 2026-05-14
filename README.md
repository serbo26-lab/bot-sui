# Bot S-UI

Bot S-UI — Telegram-бот для управления VPN-подписками на базе S-UI и sing-box.

Проект находится в активной разработке. Код и документация предназначены для изучения, тестирования и самостоятельной адаптации под свою инфраструктуру.

Бот не является готовым SaaS-продуктом “под ключ”. Для запуска требуется подготовленный сервер, установленная S-UI панель, домен, сертификаты и базовое понимание администрирования Linux/VPS.

## Что умеет бот

- Покупка и продление VPN-подписок через Telegram.
- Тарифы с разными сроками, ценами и лимитами.
- Telegram Stars и внешний платёжный провайдер для карты/СБП.
- Тестовый доступ с отдельными настройками.
- Управление remote node-серверами на sing-box.
- Подписки с несколькими локациями и протоколами VLESS / Hysteria2 / TUIC.
- Telegram proxy на remote node.
- Antiabuse по лимиту устройств/IP.
- Manual/offline подписки для клиентов без Telegram.
- Пользовательский FAQ-конструктор.
- Админская рассылка.
- Мониторинг активности пользователей.
- Production doctor, maintenance, backup/restore helpers.
- Мониторинг сертификатов и миграция домена.

## Базовая архитектура

```text
Telegram user
↓
Telegram Bot
↓
S-UI API
↓
main sing-box server
↓
remote sing-box nodes
↓
subscription links
```

Основной сервер содержит Telegram-бота, S-UI панель, SQLite-базу и конфигурацию проекта. Remote node-серверы используются для дополнительных локаций и управляются ботом через SSH.

## Что нужно подготовить заранее

Перед установкой бота рекомендуется подготовить:

- VPS с Ubuntu;
- установленную и работающую S-UI панель;
- домен, например `example.com`;
- поддомены `panel.example.com` и `sub.example.com`;
- wildcard-сертификат вида `*.example.com`;
- доступ к S-UI API;
- Telegram bot token;
- Telegram ID администратора;
- при использовании remote node — SSH-доступ к VPS-нодам.

Подробная установка S-UI/sing-box зависит от выбранной версии панели и не является основной частью этого репозитория.

## Документация

Подробные разделы находятся в каталоге [`docs/`](docs/):

- [Глоссарий](docs/00-glossary.md)
- [Обзор проекта](docs/01-overview.md)
- [Требования](docs/02-requirements.md)
- [Установка](docs/03-installation.md)
- [Конфигурация](docs/04-configuration.md)
- [Пользовательские функции](docs/05-user-guide.md)
- [Админ-панель](docs/06-admin-panel.md)
- [Тарифы и периоды](docs/07-tariffs-and-periods.md)
- [Платежи](docs/08-payments.md)
- [Тестовый доступ](docs/09-trial-access.md)
- [Manual/offline подписки](docs/10-manual-offline-subscriptions.md)
- [Remote nodes](docs/11-remote-nodes.md)
- [Telegram proxy](docs/12-telegram-proxy.md)
- [Antiabuse](docs/13-antiabuse.md)
- [Рассылки](docs/14-broadcasts.md)
- [FAQ-конструктор](docs/15-faq-builder.md)
- [Мониторинг и аналитика](docs/16-monitoring-and-analytics.md)
- [Сертификаты и домен](docs/17-certificates-and-domain.md)
- [Production doctor](docs/18-production-doctor.md)
- [Maintenance, backup и restore](docs/19-maintenance-backup-restore.md)
- [Безопасность](docs/20-security.md)
- [Troubleshooting](docs/21-troubleshooting.md)
- [Development notes](docs/22-development-notes.md)

## Важное предупреждение

Не публикуйте реальные секреты: `config.json`, `nodes.json`, `database.sqlite`, токены, приватные ключи, реальные IP, TGID админов и домены проекта.

В репозитории должны лежать только example-файлы: `config.example.json`, `nodes.example.json`, `.env.example` или аналогичные шаблоны.

## Статус проекта

Проект развивается итерационно. Перед использованием в реальной среде проверьте платежи, создание и продление подписок, remote sync, antiabuse, backup/restore, безопасность секретов и соответствие законодательству вашей страны.
