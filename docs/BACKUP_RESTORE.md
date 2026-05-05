# Backup and restore

## Manual backup

```bash
cd /opt/bot-sui
bash backup.sh
ls -lah backups
```

Each snapshot contains `database.sqlite`, `config.json`, `bot.py`, `requirements.txt`, and `health.json` when present. Logs are not backed up automatically; collect them manually only for incident analysis.

## Restore from backup

Use this only from root/sudo:

```bash
sudo /opt/bot-sui/restore.sh /opt/bot-sui/backups/YYYYMMDD_HHMMSS
```

The restore script stops `bot-sui`, saves the current database as `database.sqlite.before_restore_<timestamp>`, copies the backup database, fixes ownership, and starts the service again.

## Check backup timer

```bash
systemctl status bot-sui-backup.timer --no-pager
systemctl list-timers | grep bot-sui
```
