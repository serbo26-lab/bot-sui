# bot-sui

Telegram VPN bot for S-UI / sing-box multi-node subscriptions.

This repository snapshot is prepared for static analysis in SonarCloud.
It contains the production `bot.py`, install script, maintenance/backup scripts,
systemd unit examples, and a sanitized `config.example.json`.

Build snapshot: `stage47_tariff_periods_hotfix10_antiabuse_runtime_cap_connect_button`.

## Notes

- Do not commit real `config.json`, SQLite databases, keys, certificates, or backups.
- `config.example.json` is a sanitized template.
- Main entrypoint: `bot.py`.
