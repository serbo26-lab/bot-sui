# Сертификаты и домен

## Источник сертификата

В рекомендуемой схеме S-UI остается источником сертификата. Бот не выпускает сертификаты по умолчанию.

Задачи бота: проверить срок действия, сравнить fingerprint main и node, предупредить администратора, скопировать уже обновленный cert/key на node и пересобрать remote config при необходимости.

## Пути сертификатов

В `config.json`:

```json
"certificates": {
  "domain": "example.com",
  "source_cert": "/opt/bot-sui/certs/{domain}/fullchain.pem",
  "source_key": "/opt/bot-sui/certs/{domain}/privkey.pem",
  "remote_cert": "/root/cert-CF/{domain}/fullchain.pem",
  "remote_key": "/root/cert-CF/{domain}/privkey.pem"
}
```

`{domain}` заменяется на текущий домен.

## Node config

При deploy бот должен прописывать в sing-box config node `certificate_path` и `key_path` из текущих remote paths.

## Production doctor

Doctor может показывать текущий domain, срок действия main certificate, fingerprint, сколько node имеют совпадающий cert, warning если осталось меньше 21 дней и critical если меньше 7 дней.

## Смена домена

Общий порядок: изменить `certificates.domain` в `config.json`, убедиться что S-UI положил сертификат в source path, перезапустить bot-sui, открыть preview миграции, проверить изменения, нажать apply и дождаться remote sync/deploy.

## Preview/apply migration

Preview показывает node.domain, host, HY2/TUIC SNI, VLESS SNI если он был managed, cert paths, legacy_domains и custom domains/IP-host, которые будут пропущены.

Apply делает backup `nodes.json`, обновляет managed domains/SNI, переводит cert paths на `{domain}`, добавляет old domains в `legacy_domains`, ставит remote sync/deploy и обновляет external links.

## Безопасность

Бот не должен молча переписывать custom domain, IP-host, camouflage VLESS SNI и нестандартные node aliases.
