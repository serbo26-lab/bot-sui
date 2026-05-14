# Конфигурация

Главный файл конфигурации: `/opt/bot-sui/config.json`. Не публикуйте реальный `config.json`.

## Основные секции

### bot_token
Telegram bot token.

### admin_ids
Telegram ID администраторов.

### sui
Настройки S-UI API: `api_url`, `token`, `sub_url`, `server_name`, `server_code`, `default_inbounds`.

`default_inbounds` используются, если тариф не задает собственные S-UI inbounds.

### certificates

```json
"certificates": {
  "domain": "example.com",
  "source_cert": "/opt/bot-sui/certs/{domain}/fullchain.pem",
  "source_key": "/opt/bot-sui/certs/{domain}/privkey.pem",
  "remote_cert": "/root/cert-CF/{domain}/fullchain.pem",
  "remote_key": "/root/cert-CF/{domain}/privkey.pem",
  "auto_migrate_node_domains": true
}
```

Если домен меняется, после изменения `config.json` нужно перезапустить бота, затем использовать preview/apply миграции домена.

### remote_nodes
Управляет remote sync/deploy, nodes file, parallelism, debounce и копированием сертификатов.

### remote_antiabuse
Управляет node antiabuse, polling, TTL и auto-disable.

### telegram_proxy
Настройки Telegram proxy и mtg.

## app_settings

Часть настроек хранится в SQLite: текст `/start`, настройки тестового доступа, доступ к Telegram proxy, FAQ, рассылки и другие настройки из админки.

## После изменения config.json

```bash
sudo systemctl restart bot-sui
sudo journalctl -u bot-sui -n 100 --no-pager
```
