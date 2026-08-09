# Production security runbook

## Обязательные секреты

Production settings останавливают запуск при отсутствии или небезопасном значении:

- `DJANGO_SECRET_KEY` — случайная строка не короче 50 символов;
- `DATABASE_URL` и отдельные `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`;
- `CACHE_REDIS_PASSWORD` и `CACHE_REDIS_URL` для eviction-cache;
- отдельный `CELERY_REDIS_PASSWORD`, `CELERY_BROKER_URL`,
  `CELERY_RESULT_BACKEND` и `COORDINATION_REDIS_URL` для durable Redis;
- `FIELD_ENCRYPTION_KEYS` либо `FIELD_ENCRYPTION_KEY` с валидным Fernet-ключом;
- `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, HTTPS `SITE_URL` и `FRONTEND_URL`.

Генерация значений без сохранения в shell history:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Секреты должны храниться в secret manager или защищённом `.env` на сервере. `.env`
не коммитится в Git.

## Redis и миграция Celery broker

Production использует два физических Redis-процесса:

- `redis` — только Django cache, `allkeys-lru`, данные могут вытесняться;
- `redis_broker` — Celery broker (DB 0), result backend (DB 1) и coordination
  locks (DB 2), AOF `everysec`, volume и `noeviction`.

Разные logical DB одного процесса не изолируют eviction и persistence, поэтому
cache запрещено направлять на endpoint broker-а. Production settings проверяют
схему, пароль, endpoint-ы и уникальность DB до запуска приложения.

Первое переключение нельзя выполнять простым изменением URL: сообщения останутся
в legacy Redis незамеченными. Нужен maintenance window:

1. отключить автодеплой и остановить Celery Beat/новую постановку задач;
2. дождаться завершения active задач и опустошения всех legacy queues;
3. отдельно проверить отложенные/ETA задачи; bulk product jobs теперь
   восстанавливаются DB-dispatcher-ом и не зависят от длинного Redis countdown;
4. заполнить четыре production URL (broker DB 0, result DB 1, coordination DB 2),
   два разных пароля и создать `.deploy.env`;
5. только после подтверждённого drain установить
   `PROD_BROKER_MIGRATION_CONFIRMED=true` и разрешить deploy;
6. проверить named ping обоих workers, Beat heartbeat и тестовую задачу; старый
   broker удалять только после контрольного периода.

Глобальный `acks_late` намеренно не включён: задачи с платежами, AI и внешними
побочными эффектами требуют собственных idempotency keys. Late ACK включён только
для DB-восстанавливаемого dispatcher/bulk orchestration. Результаты по умолчанию
не сохраняются; исключение — image search, чей статус опрашивается через API.

## Ротация Fernet-ключа без простоя

1. Сгенерировать новый ключ.
2. Установить `FIELD_ENCRYPTION_KEYS=<new>,<old>` и развернуть приложение.
3. Проверить расшифровку: `python manage.py rotate_encryption_keys --dry-run`.
4. Выполнить `python manage.py rotate_encryption_keys` — все credentials и webhook
   secrets будут перешифрованы первым ключом.
5. Удалить старый ключ из `FIELD_ENCRYPTION_KEYS` и развернуть повторно.

Команда не выводит расшифрованные значения.

## Webhook delivery

Событие и все его доставки сначала сохраняются в PostgreSQL. Недоступность Redis
не теряет событие: периодическая задача повторно подбирает outbox каждую минуту.

Поддерживаемые заголовки:

- `X-MAP-Event` — тип события;
- `X-MAP-Delivery` — UUID события;
- `X-MAP-Signature` — `sha256=<HMAC-SHA256(raw_body, endpoint_secret)>`.

Успехом считается любой HTTP `2xx`. Redirect запрещён. Повторы выполняются с
экспоненциальными задержками до `WEBHOOK_MAX_ATTEMPTS`; история хранится
`WEBHOOK_AUDIT_RETENTION_DAYS`.

## Soft-delete и retention

Товары, листинги, аккаунты маркетплейсов, источники данных и webhook endpoint-ы
сначала скрываются через `deleted_at`. Восстановление:

```bash
python manage.py restore_soft_deleted products.Product 123
python manage.py restore_soft_deleted marketplaces.MarketplaceAccount 42 --reactivate
```

Проверка будущего физического удаления:

```bash
python manage.py purge_retained_data --dry-run
```

Физическая очистка запускается Celery Beat ежедневно. Сроки задаются переменными
`SOFT_DELETE_RETENTION_DAYS`, `WEBHOOK_AUDIT_RETENTION_DAYS`,
`BILLING_AUDIT_RETENTION_DAYS` и `SYNC_LOG_RETENTION_DAYS`.

## Egress policy

Production-контейнеры находятся во внутренней Docker-сети без прямого маршрута в
интернет. Исходящие HTTP(S)-запросы проходят через Squid. Proxy запрещает private,
loopback, link-local, multicast и служебные диапазоны, но разрешает публичные
endpoint-ы, необходимые Avito, AI-провайдерам, S3 и tenant webhook-ам.

`NO_PROXY` содержит только внутренние имена `db`, `redis`, `redis_broker`,
`django`, `frontend`, `nginx` и `egress_proxy`. Изменять этот список следует только
вместе с threat review.

## Deployment

Workflow `Deploy` запускается событием `workflow_run` только после успешного CI для
`push` в `main` и передаёт на сервер точный 40-символьный commit SHA. `deploy.sh`
проверяет принадлежность SHA ветке `origin/main` и разворачивает именно этот commit,
а не текущее состояние ветки на момент подключения к серверу.

В GitHub необходимо создать защищённое environment `production`, включить required
reviewers и определить:

- repository variable `PROD_DEPLOY_ENABLED=true`;
- secrets `PROD_HOST`, `PROD_USER`, `PROD_SSH_KEY`;
- secret `PROD_HOST_FINGERPRINT` с SHA256 fingerprint SSH host key.

Деплои сериализованы через concurrency group `production-deploy`. `deploy.sh`
выполняет `docker compose config --quiet` до пересборки, а CI отклоняет изменения
при branch coverage ниже 70%. После изменения production-секретов сначала
проверьте конфигурацию вручную и только затем разрешайте deploy в environment.

На сервере скопируйте `.deploy.env.example` в `.deploy.env`, направьте
`PROD_SMOKE_URL` на публичный `/api/v1/ready/` и ограничьте доступ к файлу
(`chmod 600`). `/api/v1/live/` проверяет только HTTP-процесс, а readiness также
проверяет PostgreSQL и cache. Скрипт:

1. блокирует параллельный ручной запуск и проверяет SHA, чистоту working tree,
   Compose-конфигурацию, доступность Docker и запас диска;
2. сохраняет image ID текущего release и собирает новый до переключения web/worker
   процессов;
3. выполняет `check --deploy`, migration plan и миграции в one-shot контейнере до
   запуска нового Django;
4. ждёт readiness всех сервисов и проверяет публичный HTTPS endpoint;
5. при ошибке возвращает application-сервисы на предыдущие сохранённые образы.

Глобальные `docker * prune` намеренно не выполняются: один deploy не должен удалять
ресурсы других Compose-проектов на том же хосте. Миграции БД автоматически не
откатываются, поэтому production-миграции должны следовать expand/contract-подходу
и оставаться совместимыми с предыдущей версией приложения. Ошибка pre-deploy
backup в дальнейшем должна блокировать миграцию.
