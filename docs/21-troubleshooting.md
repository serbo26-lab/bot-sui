# Troubleshooting

Этот раздел содержит команды для диагностики Bot S-UI.

Команды предполагают путь `/opt/bot-sui` и service `bot-sui`.

## Статус бота

```bash
sudo systemctl status bot-sui --no-pager
```

## Запустить бота

```bash
sudo systemctl start bot-sui
```

## Остановить бота

```bash
sudo systemctl stop bot-sui
```

## Перезапустить бота

```bash
sudo systemctl restart bot-sui
```

## Включить автозапуск

```bash
sudo systemctl enable bot-sui
```

## Отключить автозапуск

```bash
sudo systemctl disable bot-sui
```

## Логи бота

Последние 100 строк:

```bash
sudo journalctl -u bot-sui -n 100 --no-pager
```

Следить в реальном времени:

```bash
sudo journalctl -u bot-sui -f
```

Ошибки:

```bash
sudo journalctl -u bot-sui -p err -n 200 --no-pager
```

## Проверить синтаксис bot.py

```bash
cd /opt/bot-sui
sudo -u bot-sui ./venv/bin/python -m py_compile bot.py
```

## Проверить build

```bash
grep -n "APP_BUILD" /opt/bot-sui/bot.py | head
```

## Проверить config.json

```bash
cd /opt/bot-sui
sudo -u bot-sui jq . config.json >/dev/null && echo OK
```

Показать домен сертификата:

```bash
sudo -u bot-sui jq '.certificates.domain' /opt/bot-sui/config.json
```

## Проверить nodes.json

```bash
cd /opt/bot-sui
sudo -u bot-sui jq . nodes.json >/dev/null && echo OK
```

Показать node:

```bash
sudo -u bot-sui jq '.nodes[] | {id, enabled, host, domain}' /opt/bot-sui/nodes.json
```

Проверить сертификатные paths:

```bash
sudo -u bot-sui jq '.certificates // empty' /opt/bot-sui/nodes.json
```

## Проверить SQLite

```bash
cd /opt/bot-sui
sudo -u bot-sui sqlite3 database.sqlite ".tables"
```

Размер базы:

```bash
ls -lh /opt/bot-sui/database.sqlite
```

Проверка integrity:

```bash
sudo -u bot-sui sqlite3 /opt/bot-sui/database.sqlite "PRAGMA integrity_check;"
```

## Проверить maintenance timer

```bash
sudo systemctl status bot-sui-maintenance.timer --no-pager
sudo systemctl list-timers | grep bot-sui
```

Запустить maintenance вручную:

```bash
cd /opt/bot-sui
sudo -u bot-sui ./maintenance.sh
```

или:

```bash
sudo systemctl start bot-sui-maintenance.service
```

## Проверить S-UI service

Название сервиса зависит от установки.

Частые варианты:

```bash
sudo systemctl status s-ui --no-pager
sudo journalctl -u s-ui -n 100 --no-pager
```

Если S-UI service называется иначе:

```bash
systemctl list-units --type=service | grep -i sui
systemctl list-units --type=service | grep -i sing
```

## Проверить sing-box на main

```bash
systemctl list-units --type=service | grep -i sing
sudo journalctl -u sing-box -n 100 --no-pager
```

## Проверить доступ к S-UI API URL

```bash
curl -I https://panel.example.com/
```

Проверить DNS/TLS:

```bash
dig panel.example.com
openssl s_client -connect panel.example.com:443 -servername panel.example.com </dev/null 2>/dev/null | openssl x509 -noout -dates -subject
```

## Проверить сертификат на main

```bash
openssl x509 -in /opt/bot-sui/certs/example.com/fullchain.pem -noout -dates -subject -issuer
```

Fingerprint:

```bash
openssl x509 -in /opt/bot-sui/certs/example.com/fullchain.pem -noout -fingerprint -sha256
```

## Проверить сертификат по домену

```bash
echo | openssl s_client -connect node1.example.com:443 -servername node1.example.com 2>/dev/null | openssl x509 -noout -dates -subject -issuer
```

## Проверить SSH к node

```bash
ssh -i /opt/bot-sui/keys/nodes_ed25519 root@203.0.113.10
```

или:

```bash
ssh -i /opt/bot-sui/keys/nodes_ed25519 root@node1.example.com
```

## Проверить sing-box на node

На node:

```bash
sudo systemctl status sing-box --no-pager
sudo journalctl -u sing-box -n 100 --no-pager
sudo sing-box check -c /etc/sing-box/config.json
```

Путь config может отличаться.

## Проверить порты на node

```bash
ss -tulpn
sudo ufw status
sudo iptables -S
```

## Проверить DNS node

```bash
dig node1.example.com
```

## no such table

Обычно значит, что таблица не была создана при startup.

Проверить:

- вызвана ли init-функция;
- применена ли последняя версия;
- не используется ли другая база.

## no such column

Обычно значит неправильный порядок миграции.

Правильно:

```text
1. CREATE TABLE IF NOT EXISTS
2. PRAGMA table_info
3. ALTER TABLE ADD COLUMN
4. CREATE INDEX
5. SELECT/UPDATE
```

## NameError

`py_compile` не всегда ловит такие ошибки, если функция вызывается только по callback.

Что делать:

- найти строку из traceback;
- проверить, существует ли helper;
- проверить imports;
- проверить callback handler.

## Payment apply_failed

Проверить:

```text
🛠 Админ → 💳 Платежи → Apply failed
```

В логах:

```bash
sudo journalctl -u bot-sui -n 300 --no-pager | grep -i "apply_failed\|payment\|platega\|stars"
```

## Links не обновились

Проверить:

```text
🩺 Проверка production
🔁 Сверка локаций и ссылок
🔗 Восстановить ссылки локаций
🔄 Запустить sync локаций
```

В логах:

```bash
sudo journalctl -u bot-sui -n 300 --no-pager | grep -i "remote\|sync\|deploy\|links"
```

## Domain migration не сработала

Проверить:

```bash
sudo -u bot-sui jq '.certificates.domain' /opt/bot-sui/config.json
sudo -u bot-sui jq '.nodes[] | {id, domain, host, protocols}' /opt/bot-sui/nodes.json
```

Убедиться:

- bot-sui перезапущен после изменения config;
- нажата Apply migration;
- node domain не custom;
- host не IP;
- VLESS SNI не camouflage.

## Telegram proxy не показывается

Проверить:

- включен ли показ proxy;
- режим доступа paid_only / paid_or_trial;
- активна ли подписка;
- активен ли trial;
- не истек ли proxy record;
- proxy включен в админке.

## Рассылка не дошла

Проверить:

- выбранную аудиторию;
- есть ли у пользователя active paid/trial;
- привязана ли manual/offline подписка к TGID;
- не является ли пользователь админом;
- не заблокировал ли пользователь бота.

## Быстрый набор команд после обновления

```bash
cd /opt/bot-sui
sudo -u bot-sui ./venv/bin/python -m py_compile bot.py
sudo systemctl restart bot-sui
sudo journalctl -u bot-sui -n 100 --no-pager
```

В боте:

```text
🩺 Проверка production
🔁 Сверка локаций и ссылок
```
