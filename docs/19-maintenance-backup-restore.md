# Maintenance, backup и restore

## Maintenance

`maintenance.sh` может выполнять SQLite backup, cleanup старых событий, проверку размера базы, очистку логов и health snapshot.

## Systemd timer

Для регулярного запуска можно использовать `bot-sui-maintenance.service` и `bot-sui-maintenance.timer`.

## Что включать в backup

Минимально: `bot.py`, `config.json`, `nodes.json`, `database.sqlite`, `remote_client_credentials.json` если используется, `certs/` и systemd файлы.

## Пример backup

```bash
cd /opt/bot-sui
sudo -u bot-sui ./maintenance.sh
sudo tar -czf /root/bot-sui-backup-$(date +%F-%H%M).tar.gz   /opt/bot-sui/bot.py   /opt/bot-sui/config.json   /opt/bot-sui/nodes.json   /opt/bot-sui/database.sqlite   /opt/bot-sui/certs   /etc/systemd/system/bot-sui.service
```

Сообщение tar `Removing leading '/'` нормально.

## Restore checklist

После восстановления проверьте владельца файлов, права на config/secrets, `py_compile`, service, journalctl, production doctor, S-UI API, remote reconciliation и тестовый сценарий.

## Перенос VPS

При переносе важно не потерять database, nodes config, credentials, certificates, SSH keys и systemd units.
