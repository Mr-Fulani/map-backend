# Observability runbook

## Текущий статус

В приложении реализован **OBS-001 foundation**: bounded snapshot Celery broker,
метрики жизненного цикла задач, импорта datasource и основных HTTP-вызовов
Avito. Это ещё не законченный production monitoring: репозиторий не создаёт
Sentry dashboards/alert rules, а внешний dead-man и test-fire остаются
обязательной операционной настройкой.

Telemetry fail-open: сбой или отсутствие Sentry не меняет результат бизнес-
операции. Исключение — существующие coordination-механизмы продукта; их отказ
может быть fail-closed там, где это требуется для лимитов или идемпотентности.

## Celery snapshot

`collect_celery_observability` запускается Beat раз в 60 секунд в очереди
`notifications`, имеет expiration 50 секунд, soft/hard time limits 12/15 секунд
и сохраняет snapshot в coordination Redis на 150 секунд. Staff-only страница
`/admin/stats/` только читает этот кэш: HTTP-запрос не вызывает Celery inspect и
не сканирует broker.

Collector выполняет фиксированные `LLEN` и `LINDEX -1` для каждой объявленной
очереди и четырёх priority buckets. Используются только точные physical keys с
`global_keyprefix`; `KEYS`, `SCAN`, `LRANGE` и `inspect.reserved()` запрещены.
Стоимость по числу Redis-команд не зависит от глубины backlog.

Семантика полей:

- `ready_depth` — только сообщения в Redis lists, готовые к выдаче worker;
- `oldest_ready_age_seconds` — возраст текущей публикации/retry самого старого
  хвостового сообщения;
- `age_status=empty` — ready backlog пуст;
- `age_status=known` — timestamp валиден во всех непустых priority buckets;
- `age_status=unknown` — legacy/malformed/future timestamp; это не ноль и
  переводит collector в `degraded`;
- `subscribed_workers` — число ответивших worker nodes, подписанных на queue;
- `active_count` и `max_active_age_seconds` — выполняемые сейчас задачи по
  безопасному `inspect.active(safe=True)`.

`ready_depth` не включает active, reserved/prefetched, unacked, ETA/countdown и
PostgreSQL due work. Эти классы нельзя складывать в одну «общую глубину».
Timestamp `map_first_published_at_ms` сохраняется через retry, но наружу пока не
экспортируется; `map_enqueued_at_ms` обновляется при каждой публикации.

## Каталог метрик

Collector:

- `map.celery.collector.heartbeat`;
- `map.celery.collector.broker_up`;
- `map.celery.collector.worker_inspect_up`;
- `map.celery.collector.cache_up`.

Очереди и задачи:

- `map.celery.queue.depth {queue}`;
- `map.celery.queue.age_known {queue}`;
- `map.celery.queue.oldest_ready_age {queue}`;
- `map.celery.queue.subscribed_workers {queue}`;
- `map.celery.queue.active_count {queue}`;
- `map.celery.queue.max_active_age {queue}`;
- `map.celery.task.execution {task_family,queue,outcome}`;
- `map.celery.task.runtime {task_family,queue,outcome}`.

Datasource import и Avito:

- `map.sync.attempt {source_type,outcome}`;
- `map.sync.attempt.duration {source_type,outcome}`;
- `map.sync.items {source_type,result}`;
- `map.provider.request {provider,operation,outcome,response_class}`;
- `map.provider.request.duration {provider,operation,outcome,response_class}`;
- `map.provider.rate_limit {provider,operation,rate_limit_source}`.

Sync coverage сейчас относится к `import_from_datasource`; provider coverage —
к основному `AvitoAdapter`. OAuth token exchange, brand sync, `SyncRun` age,
oldest due listing, outbox backlog, feed RSS, DB saturation, backup/PITR и deploy
phase timings пока не являются сигналами этого foundation.

## Privacy и cardinality

Metric attributes проходят закрытый allowlist в приложении и повторную очистку
`before_send_metric` после того, как Sentry применил scope attributes. Разрешены
только объявленная queue и фиксированные enums task family/outcome/provider/
operation/response class. Не отправляются tenant/account/listing/task IDs,
hostname, URL/query, credentials, arguments, exception type или text. Неизвестное
значение сворачивается в `other`.

Новая metric dimension требует review и contract test. IDs допустимы в bounded
process-local timer map для сопоставления `prerun/postrun`, но не как labels.

## Production setup и начальные alerts

Без `SENTRY_DSN` приложение продолжает работать и staff snapshot может быть
доступен, но внешние `map.*` series не экспортируются. После deploy необходимо
проверить свежий heartbeat и создать внешние dashboards/alerts.

Стартовые правила (уточнить после profiling):

- missing `map.celery.collector.heartbeat` дольше 150 секунд — critical;
- `broker_up=0`, `worker_inspect_up=0` или `cache_up=0` два samples подряд —
  critical;
- `subscribed_workers=0` у любой production queue два samples подряд — critical;
- `age_known=0` при `depth>0` — degraded/critical, а не healthy zero;
- oldest age: billing/notifications/avito_price warning 120 с, critical 300 с;
  marketplace/sync warning 300 с, critical 900 с; AI/image/media/bulk warning
  900 с, critical 3600 с;
- provider `5xx`, `network_error`, `429`, task failure/retry — alert по ratio с
  минимальным числом событий, не по единичному ожидаемому сбою.

Collector сам зависит от Beat и consumer очереди `notifications`, поэтому Sentry
missing-data alert — обязательный, но не достаточный dead-man. Независимый
hourly production monitor имеет OPS-001 deadline-contract 25/17/5 минут,
bounded network calls и ручные fault modes `fail`/`timeout`. Он покрывает public
readiness, backup freshness, topology и capacity, но сам зависит от GitHub
Actions/API и потому не заменяет независимый внешний dead-man.

## Post-deploy verification

1. Убедиться, что `collect_celery_observability` enabled с interval 60 секунд и
   queue `notifications` имеет consumer.
2. За две минуты увидеть свежий `map.celery.collector.heartbeat`.
3. Открыть `/admin/stats/`: snapshot моложе 150 секунд, broker/workers/cache —
   `ok`, каждая queue имеет хотя бы одного subscriber.
4. Проверить, что stale snapshot и `age_status=unknown` не отображаются как
   пустая здоровая очередь.
5. Выполнить test-fire missing-data и degraded alert, подтвердить routing и
   назначенного on-call owner.

До выполнения пунктов 2–5 OBS-001 считается частично внедрённым foundation, а
не доказанной production-наблюдаемостью.
