# Production doctor

Production doctor — безопасная проверка состояния проекта. Он помогает увидеть проблемы до того, как они станут критичными.

## Что проверять

Рекомендуемые блоки: config, database, S-UI API, payments, remote nodes, reconciliation, antiabuse, maintenance, Telegram proxy, certificates, backup/restore readiness.

## Статусы

Обычно используются OK, warning и critical.

## Сертификаты

Блок сертификатов может показывать:

```text
Certificates: domain example.com · main OK 75d · fp ABCDEF... · nodes 5/5
```

Если node cert отличается от main, doctor должен показать warning. Если source cert отсутствует или истекает, doctor должен показать warning/critical.

## Action-кнопки

Doctor сам по себе должен быть безопасным. Если нужны действия, их лучше делать отдельными кнопками: copy certs to nodes, force remote sync, open reconciliation, open domain migration preview.

## Message length

Doctor не должен раздуваться до лимита Telegram. Подробные данные лучше выносить в отдельные экраны или TXT-файлы.
