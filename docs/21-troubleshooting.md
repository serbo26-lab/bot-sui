# Troubleshooting

Проверка сервиса:

```bash
sudo systemctl status bot-sui --no-pager
sudo journalctl -u bot-sui -n 200 --no-pager
cd /opt/bot-sui
./venv/bin/python -m py_compile bot.py
```

Типовые ошибки: no such table, no such column, NameError, S-UI API недоступен, remote SSH timeout, links не обновились, certificate mismatch, domain migration не сработала.
