# Production doctor

Production doctor — один из самых важных разделов Bot S-UI.

Он нужен, чтобы администратор мог быстро понять, готова ли система к работе и нет ли критичных проблем.

## Где находится

```text
🛠 Админ → 🩺 Проверка production
```

## Что делает doctor

Doctor выполняет безопасные проверки.

Он не должен неожиданно менять production-состояние. Если нужна операция, она должна быть отдельной кнопкой.

## Основные проверки

Doctor может проверять:

- текущий build;
- наличие config.json;
- валидность config;
- подключение к SQLite;
- размер базы;
- S-UI API;
- количество S-UI clients;
- payments;
- pending/apply_failed;
- remote nodes;
- nodes.json;
- remote reconciliation;
- antiabuse;
- maintenance;
- certificates;
- backup readiness;
- service/timer files.

## Пример строки

```text
✅ S-UI API: OK
✅ DB: OK
⚠️ Payments: pending 2
✅ Certificates: domain example.com · main OK 75d · fp ABCDEF... · nodes 5/5
```

## Кнопки doctor

### 🔄 Обновить

Повторяет проверку.

### 🔁 Сверка локаций и ссылок

Проверяет remote reconciliation.

Сравнивает:

- S-UI clients;
- local credentials;
- node configs;
- managed external links;
- subscription URL.

### 🔗 Восстановить ссылки локаций

Принудительно восстанавливает managed remote links.

Полезно, если:

- S-UI links пустые;
- external links потерялись;
- domain migration изменила ссылки;
- node protocol settings изменились.

### 🧹 Очистка хвостов

Ищет и удаляет stale managed data:

- ghost remote names;
- stale credentials;
- старые antiabuse events;
- старые managed links.

Не должна удалять активных клиентов без проверки.

### 🔄 Запустить sync локаций

Ставит remote sync/deploy в очередь.

Это нужно после изменения node, изменения cert/domain, изменения протоколов, массовых правок или восстановления.

### 💳 Аудит зависших оплат

Проверяет pending/apply_failed платежи.

Помогает найти случаи, где оплата есть, а подписка не применена.

### 🧬 Миграция server_code

Используется, если изменился технический server_code.

Должна иметь preview и apply, потому что затрагивает managed identity.

## Сертификаты

Doctor проверяет:

- source cert на main;
- срок действия;
- fingerprint;
- cert/key на node;
- совпадение node fingerprint с main.

Пороги:

```text
< 21 дней → warning
< 7 дней → critical
```

Если node cert отличается от main, нужно скопировать обновленный cert/key и сделать remote sync/reload.

## Remote reconciliation

Doctor должен помогать выявлять:

```text
missing credentials
stale credentials
missing on nodes
ghost on nodes
link issues
API links mismatch
node read errors
```

## Payments

Doctor должен подсвечивать:

- много pending;
- apply_failed;
- provider errors;
- старые неоплаченные платежи.

## Antiabuse

Doctor может проверять:

- свежесть MAIN data;
- свежесть node summaries;
- collector status;
- warnings DB size;
- stale events;
- auto-disable status.

## Maintenance

Doctor может проверять:

- запущен ли maintenance timer;
- когда был последний maintenance;
- не превышены ли лимиты таблиц;
- есть ли backup.

## Размер сообщения

Doctor не должен превышать лимит Telegram.

Если данных много, doctor должен показывать краткую строку и давать отдельную кнопку для деталей или TXT-файла.

## Что проверить после правок doctor

```text
1. Doctor открывается.
2. Не падает на missing variables.
3. Certificates отображаются.
4. Reconciliation запускается.
5. Sync запускается.
6. Payment audit открывается.
7. Сообщение не слишком длинное.
```
