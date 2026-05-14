# Maintenance, backup и restore

Этот раздел описывает обслуживание Bot S-UI, резервные копии и восстановление.

## Зачем нужен maintenance

Maintenance нужен, чтобы система не накапливала мусор и имела свежие backup.

Он может делать:

- SQLite backup;
- cleanup старых events;
- cleanup antiabuse tables;
- cleanup payment events;
- проверку размера базы;
- health snapshot;
- cleanup logs.

## Виды backup

В проекте может быть несколько видов backup.

### SQLite backup

Резервная копия базы через SQLite backup API или безопасное копирование.

Нужно для users, payments, settings, manual subscriptions, antiabuse, FAQ и support tickets.

### File backup

Архив ключевых файлов:

```text
bot.py
config.json
nodes.json
database.sqlite
remote_client_credentials.json
certs/
keys/
systemd service/timer
```

### Migration backup

Перед опасными операциями, например domain migration, бот должен сделать backup `nodes.json`.

Пример:

```text
nodes.json.bak.2026-01-01-120000
```

### Remote node backup

Может включать текущий sing-box config, service unit, cert/key paths и node antiabuse collector files.

## maintenance.sh

Если используется `maintenance.sh`, его можно запускать вручную:

```bash
cd /opt/bot-sui
sudo -u bot-sui ./maintenance.sh
```

## systemd timer

Типовые юниты:

```text
bot-sui-maintenance.service
bot-sui-maintenance.timer
```

Проверка:

```bash
sudo systemctl status bot-sui-maintenance.timer --no-pager
sudo systemctl list-timers | grep bot-sui
```

Запуск вручную:

```bash
sudo systemctl start bot-sui-maintenance.service
```

## Полный backup вручную

Пример:

```bash
cd /opt/bot-sui

sudo -u bot-sui ./maintenance.sh

sudo tar -czf /root/bot-sui-backup-$(date +%F-%H%M).tar.gz \
  /opt/bot-sui/bot.py \
  /opt/bot-sui/config.json \
  /opt/bot-sui/nodes.json \
  /opt/bot-sui/database.sqlite \
  /opt/bot-sui/remote_client_credentials.json \
  /opt/bot-sui/certs \
  /opt/bot-sui/keys \
  /etc/systemd/system/bot-sui.service \
  /etc/systemd/system/bot-sui-maintenance.timer \
  /etc/systemd/system/bot-sui-maintenance.service
```

Сообщение `Removing leading '/'` нормально для `tar`.

## Что обязательно включать в backup

Минимум:

```text
config.json
nodes.json
database.sqlite
remote_client_credentials.json
certs/
keys/
bot.py или release artifact
systemd units
```

Без `database.sqlite` потеряются пользователи, платежи, FAQ, manual/offline subscriptions и настройки.

Без `nodes.json` потеряются node domains, keys, protocols и SNI.

Без `keys/` может сломаться SSH к node.

## Restore

Общий порядок восстановления:

1. Установить Ubuntu/VPS.
2. Создать пользователя `bot-sui`.
3. Установить Python/venv/deps.
4. Распаковать backup.
5. Проверить владельца файлов.
6. Проверить права secrets.
7. Установить systemd units.
8. Перезапустить daemon.
9. Запустить service.
10. Проверить логи.
11. Запустить production doctor.
12. Запустить remote reconciliation.

## Права после restore

```bash
sudo chown -R bot-sui:bot-sui /opt/bot-sui
sudo chmod 600 /opt/bot-sui/config.json
sudo chmod 600 /opt/bot-sui/nodes.json
sudo chmod 600 /opt/bot-sui/keys/*
```

## Проверка после restore

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

## Перенос VPS

При переносе на другой VPS проверьте:

- DNS;
- firewall;
- Cloudflare proxy mode;
- S-UI API URL;
- certificates source path;
- SSH key для node;
- systemd units;
- backup restore.

## Cleanup antiabuse/events

Если antiabuse tables растут слишком быстро, maintenance должен ограничивать размер таблиц.

Важно не удалять данные, которые нужны для активных ограничений или свежих кейсов.
