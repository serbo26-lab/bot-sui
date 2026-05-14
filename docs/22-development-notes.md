# Development notes

## Статус

Проект находится в активной разработке. Перед публичным использованием каждую сборку нужно проверять.

## Подход к hotfix

Hotfix должен быть точечным: не менять платежи без причины, не ломать S-UI payload, не менять remote sync без проверки и не трогать production, если нет критичной причины.

## Stable_48 backlog

Рекомендуемый порядок крупной технической работы: базовые автотесты, дробление большого `bot.py` на модули, async cleanup, Sonar/code cleanup.

## Автотесты

Минимально полезные тесты: тарифы и периоды, покупка/продление, referral discount, trial settings, node protocol/link generation, FAQ rendering, Double VPN UX, payment payload/result_payload, apply_failed retry.

## Async cleanup

Бот на aiogram работает через async/await. Блокирующие операции внутри async handlers могут подвешивать event loop.

Особенно важно проверить remote sync/deploy, backup/restore, journalctl/diagnostics, antiabuse diagnostics, maintenance, массовые рассылки и большие файлы.

Используйте `asyncio.create_subprocess_exec`, `asyncio.to_thread` и background workers/queues.

## Публичная разработка

Для GitHub: не публиковать секреты, использовать example configs, писать нейтральную документацию, не обещать готовый коммерческий продукт и явно указывать, что проект требует настройки и проверки.
