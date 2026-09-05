# Ozon: состояние синхронизации и уведомления — CODE_READY

Активный пакет 2026-09-06. Результат: в настройках каждого Ozon-кабинета
видны состояние фоновой синхронизации и последний успешный запуск; устойчивые
сбои и подтверждённое восстановление сообщаются через существующие настройки
уведомлений тенанта без повторного спама.

Границы: Ozon monitoring/presentation и подключение к существующей durable
доставке уведомлений. Существующие очереди используются без новых режимов.
Avito публикация/feed, заказы/pagination, аналитика и CI/image delivery не меняются.
Не выполняются provider writes, включение автоматизации и очистка диска.

Проверки: disabled/idle/unknown/pending/success/error/stale, отсутствие тревоги
на краткий сбой, восстановление только после успеха, дедупликация и cooldown,
изоляция tenant/account, скрытие provider payloads, потеря broker и повторная
доставка, fair bounded monitor, migration upgrade, полный CI и read-only UI smoke.

Порог задержки/устойчивого сбоя — 15 минут. Предупреждение срока ключа —
7 дней; истёкший ключ требует действия сразу. Восстановление подтверждается
успешным запуском после начала инцидента и двумя минутами без проблемы.
Уведомления об ошибках не чаще одного за 30 минут на кабинет; без изменения
инцидента повторов нет. Выключение автоматизации не считается восстановлением.

Монитор не обращается к Ozon; UI читает локальный снимок, отдельное обновление
диагностики также не выполняет provider I/O. Отсутствие worker-снимка — неизвестное
состояние, а не доказательство остановки. Полная остановка workers контролируется
существующим внешним Sentry dead-man контуром: уведомление через ту же очередь
не может гарантированно отправиться при полном отказе очереди.

Предыдущий этап (очередность обхода и общие логи) завершён и выложен:
PR #317, SHA a2308d566511d652002c383b82c8b737349bdb7a. Старый CODE_READY в
OZON_OPERATIONS_RELEASE.md описывает состояние до окончательного CI; итоговые
release evidence и production UI проверки записаны в PR #317.

## Проверки текущего пакета (2026-09-06)

Локально, Python 3.12 / отдельная PostgreSQL 14 (финальный CI — PostgreSQL 16):

- `pytest apps/marketplaces/tests/test_ozon_health.py apps/marketplaces/tests/test_ozon_fair_sync.py apps/marketplaces/tests/test_ozon_events.py apps/marketplaces/tests/test_ozon_account_api.py apps/notifications/tests -q -p no:cacheprovider --reuse-db` — **169 passed**, включая upgrade миграции 0047 и идемпотентную доставку.
- `python3 -m pytest tests/test_runtime_contract.py tests/test_healthchecks.py tests/test_deploy_contract.py -q -p no:cacheprovider` — **67 passed** (локальный Python 3.11 без pytest-django: предупреждение о настройке; эти тесты без Django).
- `npm run typecheck`, `npm run lint`, `npm run test:unit` в `frontend/` — **pass; 96 passed**.
- `mypy --check-untyped-defs --follow-imports=silent apps/marketplaces/ozon_health.py apps/marketplaces/ozon_health_monitor.py apps/marketplaces/ozon_health_views.py` — **pass**.
- `manage.py makemigrations --check --dry-run` — **No changes detected**.
- flake8 новых модулей/тестов, `git diff --check` — **pass**.

Полный gate, сборка production images, deploy и read-only production smoke ещё
не подтверждены на момент этого коммита. Окончательные SHA, CI/deploy runs и
наблюдение после выкладки записываются в PR этого пакета — CODE_READY не означает
production-релиз. Локальный полный backup integration gate не запускался из-за
недостатка свободного места; очистка не выполнялась.

Доставка в реальный Telegram проверяется только при реальном инциденте и
включённых настройках тенанта; намеренно ломать подключение для теста нельзя.
Проверка короткого повторного сбоя подтверждает сброс recovery grace; успешное
завершение последней задачи подтверждает recovery даже при опустевшей очереди.
