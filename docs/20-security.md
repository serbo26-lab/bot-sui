# Безопасность

## Не публиковать

В публичный GitHub нельзя добавлять `config.json`, `nodes.json`, `database.sqlite`, `remote_client_credentials.json`, SSH private keys, сертификаты и private key, Telegram bot token, S-UI token, Cloudflare token/global key, реальные IP, домены и TGID.

## Права файлов

```bash
chmod 600 config.json
chmod 600 nodes.json
chmod 600 /opt/bot-sui/keys/*
chmod 600 /opt/bot-sui/certs/*/privkey.pem
```

## Service user

Бот должен работать от отдельного пользователя, например `bot-sui`. Не рекомендуется запускать Telegram-бота от root без необходимости.

## SSH к node

Если бот управляет remote node, используйте отдельный SSH key. Храните его только на main-сервере.

## Cloudflare

Если используются Cloudflare tokens, лучше применять ограниченный API Token, а не Global API Key. Если S-UI использует Global Key, не публикуйте его и не переносите в документацию.

## Платежи

Платежные secrets должны храниться только в `config.json` или secrets-файлах с ограниченными правами.

## Telegram данные

TGID и username пользователей могут быть персональными данными. Учитывайте требования законодательства вашей страны.
