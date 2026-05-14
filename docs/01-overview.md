# Обзор проекта

Bot S-UI — Telegram-бот для управления VPN-подписками на базе S-UI и sing-box.

Проект находится в активной разработке и предназначен для самостоятельной адаптации под свою инфраструктуру.

## Назначение

Бот закрывает несколько задач: покупка и продление подписок, управление тарифами, remote node-локации, MultiHop / Double VPN, antiabuse, manual/offline подписки и monitoring.

## Архитектура

```text
Telegram → Bot S-UI → S-UI API → main sing-box → remote node sing-box
```

Bot S-UI не заменяет S-UI. S-UI остается источником клиентов и подписок, а бот управляет бизнес-логикой, Telegram UX, node-ссылками, antiabuse и админкой.

## MultiHop / Double VPN

Термин означает режим, где основной сервер не является финальной точкой выхода в интернет, а проксирует трафик дальше: main → remote server → internet, main → remote server → WARP или main → WARP endpoint. Режим опциональный и может быть выключен.
