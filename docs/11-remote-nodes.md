# Remote nodes

Remote node — отдельный VPS-сервер с sing-box, который используется как VPN-локация.

Бот управляет node через SSH: копирует сертификаты, генерирует sing-box config, деплоит config, перезапускает sing-box и добавляет external links к подпискам.

## nodes.json

Remote nodes описываются в `nodes.json`. Не публикуйте реальный `nodes.json`, потому что он может содержать IP, домены, Reality keys, obfs passwords, SSH paths и конфигурацию протоколов.

## Протоколы

Node может поддерживать VLESS/Reality, Hysteria2 и TUIC. Админ может включать/выключать протоколы на конкретной node. Если протокол выключен, его inbound/config/link не генерируется.

## Sync/deploy

Remote sync обновляет node config и managed external links. Sync нужен после создания, продления, отключения, удаления подписки, изменения node, изменения domain/cert paths или protocol settings.

## Reconciliation

Reconciliation сравнивает S-UI clients, local SQLite/credentials, remote node configs и external links. Это нужно, чтобы найти отсутствующие credentials, ghost clients, stale links и mismatches.

## SSH timeout

Если node недоступна из-за datacenter/SSH timeout, это не всегда баг бота. Remote sync должен использовать retry/backoff и не удалять links агрессивно из-за одной временной ошибки.

## Drain / maintenance

Node может быть временно выведена из выдачи. В таком режиме links можно убрать из подписок без удаления всей подписки.

## Domain migration

При смене домена используется preview/apply: preview показывает изменения, apply делает backup `nodes.json`, обновляет managed node domains/SNI, добавляет legacy_domains и ставит remote sync/deploy.

Custom domain и IP-host не должны переписываться молча.
