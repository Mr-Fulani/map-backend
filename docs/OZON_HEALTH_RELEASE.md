# Ozon: состояние синхронизации и уведомления — ENABLED

Обновлено: 2026-09-06. Production baseline: `04bb5732dfc1f3410dbecf4d88969b0fd945c51b`,
[PR #318](https://github.com/Mr-Fulani/map-backend/pull/318).
Общий статус интеграции: [OZON_STATUS.md](OZON_STATUS.md).

Результат: в настройках каждого Ozon-кабинета
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
состояние, а не доказательство остановки. Для полной остановки workers нужен
внешний dead-man: уведомление через ту же очередь не может гарантированно
отправиться при полном отказе очереди. Sentry check-ins реализованы, но реальная
маршрутизация внешнего алерта и test-fire не подтверждены этим релизом; порядок
проверки — [OBSERVABILITY.md](OBSERVABILITY.md).

Предыдущий этап (очередность обхода и общие логи) завершён и выложен:
[PR #317](https://github.com/Mr-Fulani/map-backend/pull/317),
SHA `a2308d566511d652002c383b82c8b737349bdb7a`.
Итоги: [OZON_OPERATIONS_RELEASE.md](OZON_OPERATIONS_RELEASE.md).

## Проверки текущего пакета (2026-09-06)

Локально, Python 3.12 / отдельная PostgreSQL 14 (финальный CI — PostgreSQL 16):

- `pytest apps/marketplaces/tests/test_ozon_health.py apps/marketplaces/tests/test_ozon_fair_sync.py apps/marketplaces/tests/test_ozon_events.py apps/marketplaces/tests/test_ozon_account_api.py apps/notifications/tests -q -p no:cacheprovider --reuse-db` — **169 passed**, включая upgrade миграции 0047 и идемпотентную доставку.
- `python3 -m pytest tests/test_runtime_contract.py tests/test_healthchecks.py tests/test_deploy_contract.py -q -p no:cacheprovider` — **67 passed** (локальный Python 3.11 без pytest-django: предупреждение о настройке; эти тесты без Django).
- `npm run typecheck`, `npm run lint`, `npm run test:unit` в `frontend/` — **pass; 96 passed**.
- `mypy --check-untyped-defs --follow-imports=silent apps/marketplaces/ozon_health.py apps/marketplaces/ozon_health_monitor.py apps/marketplaces/ozon_health_views.py` — **pass**.
- `manage.py makemigrations --check --dry-run` — **No changes detected**.
- flake8 новых модулей/тестов, `git diff --check` — **pass**.

## Итоговый release evidence

- [Полный CI 33996235535](https://github.com/Mr-Fulani/map-backend/actions/runs/33996235535) — **success**:
  backend **2995 passed, 2 skipped**, coverage **80.3%**; frontend **96 passed**,
  typecheck/lint/build; runtime/deploy contracts **67 passed**. Миграции,
  schema/types, security, сборка images и runtime gate прошли.
- [Main CI 33996973442](https://github.com/Mr-Fulani/map-backend/actions/runs/33996973442) — **success**, reuse проверенного exact tree.
- [Deploy 33996990041](https://github.com/Mr-Fulani/map-backend/actions/runs/33996990041) — **success**,
  2026-09-05 22:52:44 UTC (2026-09-06 01:52:44 UTC+3). Создан зашифрованный
  pre-migration backup, применена миграция `marketplaces.0047`, зарегистрирован
  монитор; все 10 сервисов healthy, topology и внешний readiness gate прошли.
- Read-only UI canary AlfaPro: в 01:54:18 и после обновления в 01:54:45 UTC+3
  виден свежий снимок. Сверка карточек — «Нет задач», автоматизация цен/остатков
  и заказов — «Выключено»; предупреждения о stale monitor/очереди отсутствуют.
- `product_write_enabled=true` сохранён; автоматизация цен/остатков и заказов
  осталась выключенной. Provider writes и изменения Avito не выполнялись.
- `PROD_DEPLOY_ENABLED=false` восстановлен после deploy. Это предохранитель
  новых выкладок, а не выключатель уже работающего Ozon.

Локальный полный backup integration gate не запускался из-за недостатка места;
это не отменяет успешный финальный CI/runtime gate. Очистка не выполнялась.

Telegram подключён у AlfaPro, уведомления об ошибках включены. Реальный инцидент
и тестовая отправка намеренно не создавались: end-to-end доставка в Telegram
этим canary не доказана. Намеренно ломать подключение для теста нельзя.
Проверка короткого повторного сбоя подтверждает сброс recovery grace; успешное
завершение последней задачи подтверждает recovery даже при опустевшей очереди.
