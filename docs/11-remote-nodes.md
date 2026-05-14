# Remote nodes

## Назначение

Remote node — это отдельный VPS-сервер с sing-box, который бот использует как дополнительную VPN-локацию.

Идея такая:

```text
main server
├─ Telegram bot
├─ S-UI panel
├─ SQLite database
└─ управляет remote nodes через SSH

remote node
├─ sing-box
├─ конфиг, сгенерированный ботом
├─ сертификаты, скопированные с main
└─ пользователи, добавленные ботом в config
```

Remote node не является отдельной S-UI панелью. S-UI остается на main-сервере. Бот берет клиентов из S-UI, создает для них данные подключения и сам собирает конфиг для remote node.

## Общая логика работы

Сценарий работы remote node обычно такой:

1. Админ добавляет node в `nodes.json` или через админский интерфейс.
2. Для node указываются:
   - `id`;
   - label/страна;
   - IP или host для SSH;
   - домен node, например `node1.example.com`;
   - включенные протоколы;
   - порты;
   - SNI;
   - Reality keys / HY2 obfs / TUIC параметры.
3. Админ запускает bootstrap node.
4. Бот подключается к node по SSH.
5. Бот устанавливает нужные пакеты и sing-box.
6. Бот создает директории на node.
7. Бот копирует сертификаты на node.
8. Бот генерирует sing-box config.
9. Бот открывает firewall-порты, если эта опция включена.
10. Бот запускает или перезапускает sing-box на node.
11. После этого node может выдавать VPN-ссылки пользователям.

## Bootstrap node

Bootstrap — это первичная подготовка нового VPS-node.

Обычно админ нажимает кнопку bootstrap в админке remote node. После этого бот сам выполняет подготовку сервера.

Что делает bootstrap:

```text
1. Проверяет SSH-доступ.
2. Создает рабочие директории на node.
3. Устанавливает sing-box или проверяет его наличие.
4. Копирует cert/key на node.
5. Создает systemd service для sing-box, если требуется.
6. Создает базовый config.
7. Открывает нужные firewall-порты, если включено.
8. Запускает sing-box.
9. Возвращает админу результат.
```

После успешного bootstrap node считается технически подготовленной. Дальше ее конфиг обновляется через sync/deploy.

## Deploy / sync

Deploy или sync — это обновление node после изменений.

Бот пересобирает sing-box config на основе текущего состояния:

- какие подписки активны;
- какие remote nodes включены;
- какие протоколы включены на node;
- какие тарифы имеют доступ к этой node;
- какие trial-подписки имеют доступ к этой node;
- какие клиенты отключены;
- какие клиенты удалены;
- какие сертификаты и пути используются.

После этого бот отправляет новый config на node и перезапускает/reload sing-box.

## Когда нужен sync

Sync может запускаться после:

- создания подписки;
- продления подписки;
- отключения подписки;
- включения подписки;
- удаления подписки;
- изменения тарифного node scope;
- изменения trial node scope;
- изменения протоколов node;
- изменения домена;
- изменения сертификатов;
- ручной команды админа;
- периодической reconciliation/maintenance логики.

## Создание конфига node

Node config не пишется вручную на node. Его собирает бот.

В config попадают только те клиенты и протоколы, которые должны быть активны на этой node.

Например, если на node включены все протоколы:

```text
VLESS
Hysteria2
TUIC
```

и подписка имеет доступ к этой node, бот создаст для нее remote links по включенным протоколам.

Если админ выключил TUIC на node, то:

```text
• TUIC inbound не попадет в config;
• TUIC link не будет создан;
• старый managed TUIC link должен быть очищен после sync.
```

## Протоколы

Remote node может поддерживать:

- VLESS / Reality;
- Hysteria2;
- TUIC.

Каждый протокол можно включать или выключать отдельно на конкретной node.

Это полезно, если:

- провайдер блокирует один из протоколов;
- нужно временно убрать протокол;
- нужно оставить только наиболее стабильный вариант;
- node используется для тестов.

## Ссылки пользователей

Remote node-ссылки добавляются к подписке как external links.

Пользователь обычно получает одну subscription-ссылку. Внутри нее могут быть:

- main server;
- node1;
- node2;
- node3;
- разные протоколы.

В пользовательском интерфейсе не нужно показывать технические детали вроде `managed external links`. Пользователь просто обновляет подписку в своем VPN-клиенте и видит доступные локации.

## Названия локаций

В обычной подписке названия remote links могут быть короткими:

```text
🇳🇱 Netherlands
🇵🇱 Poland
🇩🇪 Germany
```

Протокол клиентское приложение обычно показывает отдельно.

Если конкретный клиент, например Clash, требует уникальные имена, можно сделать отдельную логику имен для Clash:

```text
Netherlands · VLESS
Netherlands · HY2
Netherlands · TUIC
```

## Node scope тарифа

Тариф может давать доступ:

- ко всем node;
- только к выбранным node;
- только к main-серверу;
- к MultiHop / Double VPN без обычных remote locations.

Если тариф не имеет доступных remote nodes, блок “Локации в подписке” можно скрывать в пользовательском UX.

## Trial node scope

Тестовый доступ может иметь отдельный список node.

Например:

```text
Платный тариф:
• node1
• node2
• node3

Тестовый доступ:
• только node1
```

Также тест может быть remote-only, то есть без main/S-UI inbounds.

## Reconciliation

Reconciliation — это сверка фактического состояния.

Бот сравнивает:

- клиентов в S-UI;
- локальные credentials в SQLite/файлах;
- конфиги remote node;
- external links в подписках;
- доступные node и протоколы.

Цель reconciliation — найти расхождения:

```text
• клиент есть в S-UI, но нет credentials;
• клиент есть на node, но уже удален из S-UI;
• links устарели;
• node config не соответствует тарифу;
• protocol выключен, но link еще остался;
• S-UI API показывает links=[], но subscription URL фактически содержит node links.
```

Reconciliation не должен бездумно удалять рабочие данные. Его задача — показать проблему и, если предусмотрено, безопасно исправить managed-часть.

## Drain / maintenance mode

Drain нужен, когда node нужно временно вывести из использования.

Например:

- VPS переезжает;
- node нестабильна;
- нужно заменить домен;
- нужно обновить сервер;
- нужно убрать node из новых подписок.

В drain-режиме бот может убрать links этой node из подписок, но не удалять сами подписки.

## Сертификаты на node

Для Hysteria2 и TUIC node использует TLS-сертификат.

Бот берет cert/key с main-сервера по путям из `certificates`:

```text
/opt/bot-sui/certs/{domain}/fullchain.pem
/opt/bot-sui/certs/{domain}/privkey.pem
```

и копирует их на node, например:

```text
/root/cert-CF/{domain}/fullchain.pem
/root/cert-CF/{domain}/privkey.pem
```

Затем эти пути попадают в sing-box config node:

```text
certificate_path = /root/cert-CF/example.com/fullchain.pem
key_path = /root/cert-CF/example.com/privkey.pem
```

Бот не обязан сам выпускать сертификаты. В рекомендуемой схеме источник сертификата — S-UI, а бот только мониторит и копирует обновленные cert/key на node.

## Миграция домена

Если меняется домен проекта, нужно обновить не только cert paths, но и node domains/SNI.

Например:

```text
node1.old-domain.com → node1.new-domain.com
```

Миграция домена через preview/apply должна показать:

- старый домен;
- новый домен из `config.json`;
- какие `node.domain` будут изменены;
- какие `host` будут изменены;
- какие HY2/TUIC SNI будут изменены;
- какие VLESS SNI будут изменены;
- какие cert paths будут изменены;
- какие custom domains/IP-host будут пропущены;
- какие `legacy_domains` будут добавлены.

Apply должен:

```text
1. Создать backup nodes.json.
2. Обновить managed node domains.
3. Обновить HY2/TUIC SNI.
4. Обновить cert path templates.
5. Добавить старые домены в legacy_domains.
6. Поставить remote sync/deploy в очередь.
7. Пересобрать node config.
8. Обновить managed external links.
9. Очистить старые managed links.
```

## Что бот не должен менять молча

Бот не должен автоматически переписывать:

- IP в `host`;
- кастомные домены;
- camouflage SNI для VLESS;
- нестандартные alias-домены node;
- вручную добавленные не-managed links.

Для таких случаев нужен preview и ручное подтверждение.

## SSH timeout

Если node не отвечает по SSH, это не всегда ошибка бота.

Причины могут быть:

- проблема VPS provider;
- firewall;
- временный network issue;
- недоступность SSH;
- server reboot;
- неправильный SSH key.

Бот должен использовать retry/backoff и не делать бесконечный deploy на все node из-за одной недоступной node.

## Минимальный чеклист добавления новой node

```text
1. Создать VPS.
2. Создать DNS record node1.example.com → IP node.
3. Убедиться, что main server может подключиться по SSH.
4. Добавить node в nodes.json или через админку.
5. Проверить domain/SNI/protocol settings.
6. Нажать bootstrap.
7. Проверить результат bootstrap.
8. Запустить sync/deploy.
9. Проверить production doctor / remote reconciliation.
10. Проверить subscription link в VPN-клиенте.
```
