# Operations checklist

## Service

```bash
systemctl status bot-sui --no-pager
systemctl restart bot-sui
journalctl -u bot-sui -n 100 --no-pager
```

## Monitor

```bash
/opt/bot-sui/monitor.sh
systemctl status bot-sui-monitor.timer --no-pager
```

## Disk usage and storage hygiene

```bash
df -h /opt/bot-sui
du -sh /opt/bot-sui /opt/bot-sui/backups /opt/bot-sui/logs /opt/bot-sui/incident_logs 2>/dev/null
systemctl list-timers bot-sui-backup.timer bot-sui-maintenance.timer --no-pager
sudo -u bot-sui /opt/bot-sui/maintenance.sh
```

Production defaults: one lightweight backup per day, keep 7 snapshots; maintenance runs daily and trims old raw antiabuse logs, incident logs and accidental migration archives.

## Config

Real settings live in `/opt/bot-sui/config.json`. `config.example.json` is only a template/documentation file.

## Final release smoke checklist

Run these before handing the bot to real users:

1. Admin -> Production doctor: OK.
2. Admin -> Remote reconciliation: no stale credentials, missing users, ghost users, link issues or node read errors.
3. Payment retry audit: no stuck pending/processing/apply_failed payments.
4. Test one purchase and one renewal.
5. Verify main/S-UI connection and all enabled node locations.
6. Verify TG PROXY user link if enabled.
7. Verify Antiabuse case actions: restrict for 10 minutes and enable now.
8. Create a fresh root migration package and keep it as a secret emergency archive.
