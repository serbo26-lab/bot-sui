# Конфигурация

Этот раздел описывает, какие конфиги нужны Bot S-UI, что в них указывать и за что отвечают основные параметры.

В проекте обычно есть два главных файла:

```text
/opt/bot-sui/config.json
/opt/bot-sui/nodes.json
```

`config.json` отвечает за Telegram-бота, S-UI, платежи, сертификаты, общие настройки и фоновые задачи.

`nodes.json` отвечает за remote node-серверы: IP/host, домены, протоколы, ключи, SNI, сертификаты и SSH.

Оба файла нельзя публиковать в открытом репозитории.

## Где лежат файлы

Типовая структура:

```text
/opt/bot-sui/
├─ bot.py
├─ config.json
├─ nodes.json
├─ database.sqlite
├─ remote_client_credentials.json
├─ certs/
├─ keys/
└─ logs/
```

## config.json

Ниже пример общей структуры. Конкретные поля могут отличаться в вашей версии.

```json
{
  "bot_token": "PUT_TELEGRAM_BOT_TOKEN_HERE",
  "admin_ids": [123456789],
  "sui": {
    "api_url": "https://panel.example.com/app/apiv2",
    "token": "PUT_SUI_TOKEN_HERE",
    "sub_url": "https://sub.example.com/",
    "server_name": "🌐 Multi",
    "server_code": "MAIN",
    "default_inbounds": "1,2,3"
  },
  "certificates": {
    "domain": "example.com",
    "source_cert": "/opt/bot-sui/certs/{domain}/fullchain.pem",
    "source_key": "/opt/bot-sui/certs/{domain}/privkey.pem",
    "remote_cert": "/root/cert-CF/{domain}/fullchain.pem",
    "remote_key": "/root/cert-CF/{domain}/privkey.pem",
    "auto_migrate_node_domains": true
  },
  "remote_nodes": {
    "enabled": true,
    "nodes_file": "/opt/bot-sui/nodes.json",
    "sync_interval_seconds": 900,
    "deploy_parallelism": 5,
    "sync_debounce_seconds": 2,
    "copy_certs_on_sync": true
  },
  "remote_antiabuse": {
    "enabled": true,
    "summary_poll_seconds": 300,
    "active_ip_ttl_minutes": 10,
    "auto_disable_enabled": false
  },
  "telegram_proxy": {
    "enabled": true,
    "mtg_version": "v2.2.8",
    "default_port": 8443
  }
}
```

## bot_token

Telegram bot token. Получается в BotFather.

Если token попал в публичный репозиторий, его нужно сразу перевыпустить.

## admin_ids

Список Telegram ID администраторов.

```json
"admin_ids": [123456789, 987654321]
```

Только эти пользователи увидят кнопку `🛠 Админ`.

## sui.api_url

Адрес S-UI API.

```json
"api_url": "https://panel.example.com/app/apiv2"
```

Важно:

- URL должен быть доступен с сервера, где работает бот;
- TLS-сертификат должен быть валидным;
- Cloudflare proxy может влиять на доступ;
- путь API зависит от версии и настроек S-UI.

## sui.token

Токен S-UI API.

Бот использует его для чтения клиентов, создания подписок, продления, отключения, включения, удаления, editbulk и обновления external links.

## sui.sub_url

Базовый URL подписок.

```json
"sub_url": "https://sub.example.com/"
```

Используется для формирования subscription-ссылок.

## sui.server_name

Пользовательское название сервера.

Пример:

```json
"server_name": "🌐 Multi"
```

## sui.server_code

Технический код сервера.

```json
"server_code": "MAIN"
```

В пользовательских сообщениях technical code лучше не показывать.

Если server_code меняется после запуска, нужна отдельная миграция, чтобы старые managed данные не потеряли связь.

## sui.default_inbounds

Inbound IDs, которые используются по умолчанию, если тариф не задает свои.

```json
"default_inbounds": "1,2,3"
```

Это относится к main/S-UI части. Remote node-ссылки управляются через `nodes.json`.

## certificates

Секция сертификатов.

```json
"certificates": {
  "domain": "example.com",
  "source_cert": "/opt/bot-sui/certs/{domain}/fullchain.pem",
  "source_key": "/opt/bot-sui/certs/{domain}/privkey.pem",
  "remote_cert": "/root/cert-CF/{domain}/fullchain.pem",
  "remote_key": "/root/cert-CF/{domain}/privkey.pem"
}
```

`domain` — основной домен проекта. При смене домена меняется `certificates.domain`, затем перезапускается бот, затем выполняется preview/apply миграции домена.

`source_cert/source_key` — путь к сертификату на main-сервере. В рекомендуемой схеме S-UI или внешний certbot обновляет сертификат, а бот только читает готовый cert/key.

`remote_cert/remote_key` — путь, куда cert/key копируются на remote node. Эти же пути попадают в sing-box config node для Hysteria2/TUIC.

`{domain}` заменяется на значение `certificates.domain`.

## remote_nodes

```json
"remote_nodes": {
  "enabled": true,
  "nodes_file": "/opt/bot-sui/nodes.json",
  "sync_interval_seconds": 900,
  "deploy_parallelism": 5,
  "sync_debounce_seconds": 2,
  "copy_certs_on_sync": true,
  "open_firewall_on_sync": true
}
```

`enabled` включает remote node-функции.

`nodes_file` — путь к `nodes.json`.

`sync_interval_seconds` — периодический sync remote nodes.

`deploy_parallelism` — сколько node можно деплоить параллельно.

`sync_debounce_seconds` — задержка, чтобы несколько событий подряд не запускали несколько sync одновременно.

`copy_certs_on_sync` — копировать сертификаты на node при sync/deploy.

## remote_antiabuse

```json
"remote_antiabuse": {
  "enabled": true,
  "summary_poll_seconds": 300,
  "active_ip_ttl_minutes": 10,
  "auto_disable_enabled": false
}
```

`summary_poll_seconds` — как часто main-сервер забирает summaries с node.

`active_ip_ttl_minutes` — сколько минут IP считается активным.

`auto_disable_enabled` должен быть выключен по умолчанию.

## telegram_proxy

```json
"telegram_proxy": {
  "enabled": true,
  "mtg_version": "v2.2.8",
  "default_port": 8443
}
```

Задает базовые параметры TG proxy. Доступ пользователей к proxy дополнительно настраивается в админке.

## payments

Проект рассчитан на Telegram Stars и Platega/card/SBP-сценарий. Если нужен другой провайдер, потребуется адаптация кода.

Платежные токены и секреты не должны попадать в публичный репозиторий.

## referrals

Настройки реферальной системы.

```json
"referrals": {
  "first_friend_discount_pct": 5,
  "next_friend_discount_pct": 2.5,
  "max_discount_pct": 50,
  "require_success_payment": true
}
```

## nodes.json

`nodes.json` описывает remote node-серверы.

```json
{
  "certificates": {
    "source_cert": "/opt/bot-sui/certs/{domain}/fullchain.pem",
    "source_key": "/opt/bot-sui/certs/{domain}/privkey.pem",
    "remote_cert": "/root/cert-CF/{domain}/fullchain.pem",
    "remote_key": "/root/cert-CF/{domain}/privkey.pem"
  },
  "nodes": [
    {
      "id": "node1",
      "enabled": true,
      "label": "🇳🇱 Netherlands",
      "code": "NL",
      "host": "203.0.113.10",
      "domain": "node1.example.com",
      "ssh_user": "root",
      "ssh_key": "/opt/bot-sui/keys/nodes_ed25519",
      "protocols": {
        "vless": {
          "enabled": true,
          "port": 443,
          "sni": "www.example.com",
          "flow": "xtls-rprx-vision",
          "fingerprint": "chrome",
          "public_key": "PUT_REALITY_PUBLIC_KEY_HERE",
          "private_key": "PUT_REALITY_PRIVATE_KEY_HERE",
          "short_id": "PUT_SHORT_ID_HERE"
        },
        "hysteria2": {
          "enabled": true,
          "port": 443,
          "sni": "node1.example.com",
          "obfs_password": "PUT_OBFS_PASSWORD_HERE"
        },
        "tuic": {
          "enabled": true,
          "port": 8443,
          "sni": "node1.example.com"
        }
      },
      "legacy_domains": []
    }
  ]
}
```

## Поля node

`id` — технический ID node. Лучше использовать короткие латинские ID без пробелов.

`enabled` — включена ли node.

`label` — пользовательское название локации.

`host` — адрес для SSH-подключения. Может быть IP или домен.

`domain` — домен, который используется в ссылках пользователя.

`ssh_user/ssh_key` — SSH-доступ к node.

## protocols.vless

VLESS/Reality обычно использует Reality keys/short_id и не использует обычный TLS-сертификат как HY2/TUIC.

## protocols.hysteria2

Hysteria2 использует сертификат на node и SNI.

## protocols.tuic

TUIC использует сертификат на node и SNI.

## legacy_domains

Старые домены node. Используются после миграции домена для очистки старых managed links.

## После изменения config.json

```bash
sudo systemctl restart bot-sui
sudo journalctl -u bot-sui -n 100 --no-pager
```

## После изменения nodes.json

```bash
cd /opt/bot-sui
sudo -u bot-sui jq . nodes.json >/dev/null && echo OK
sudo systemctl restart bot-sui
```

Затем в админке выполнить remote sync/reconciliation.
