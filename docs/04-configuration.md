# Конфигурация

Главный файл: `/opt/bot-sui/config.json`. Не публикуйте реальный config.json.

Ключевые секции: bot_token, admin_ids, sui, paths, certificates, remote_nodes, remote_antiabuse, telegram_proxy, payments, referrals.

## certificates

```json
{
  "domain": "example.com",
  "source_cert": "/opt/bot-sui/certs/{domain}/fullchain.pem",
  "source_key": "/opt/bot-sui/certs/{domain}/privkey.pem",
  "remote_cert": "/root/cert-CF/{domain}/fullchain.pem",
  "remote_key": "/root/cert-CF/{domain}/privkey.pem"
}
```

После изменения config.json перезапустите bot-sui.
