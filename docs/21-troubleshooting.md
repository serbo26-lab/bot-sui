# Troubleshooting

## Бот не стартует

```bash
sudo systemctl status bot-sui --no-pager
sudo journalctl -u bot-sui -n 200 --no-pager
cd /opt/bot-sui
./venv/bin/python -m py_compile bot.py
```

## no such table

Обычно значит, что не была выполнена миграция базы или функция init не вызвана при старте.

## no such column

Обычно значит, что индекс или запрос использует новую колонку до ALTER TABLE. Нужно исправить порядок миграции: сначала добавить колонку, потом создавать индекс/делать SELECT.

## NameError

`py_compile` ловит не все runtime NameError. Нужен static scan или ручная проверка новых callback/веток.

## S-UI API недоступен

Проверьте `sui.api_url`, token, DNS, TLS, Cloudflare proxy, firewall и доступность панели с сервера.

## Remote node SSH timeout

Это может быть не баг бота, а проблема datacenter/network/SSH. Проверьте ручной SSH с main-сервера.

## Links не обновились

Проверьте remote sync, reconciliation, legacy_domains, force rewrite reason и поведение S-UI `/clients`.

## Certificates mismatch

Проверьте `certificates.domain`, source cert path, node remote cert path, production doctor, domain migration preview/apply и remote sync/deploy.

## Domain migration не сработала

Возможные причины: bot-sui не перезапущен после config.json, node domain кастомный и пропущен, host является IP, VLESS SNI кастомный camouflage, не нажата apply-кнопка.

## Пользователь не получает рассылку

Проверьте аудиторию, активность подписки/trial, Telegram binding manual/offline подписки, admin_ids и ошибки Telegram.
