# Production security runbook

## Обязательные секреты

Production-контур останавливает запуск при отсутствии или небезопасном значении:

- `DJANGO_SECRET_KEY` — случайная строка не короче 50 символов;
- `DATABASE_URL` и отдельные `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`;
- `CACHE_REDIS_PASSWORD` и `CACHE_REDIS_URL` для eviction-cache;
- отдельный `CELERY_REDIS_PASSWORD`, `CELERY_BROKER_URL`,
  `CELERY_RESULT_BACKEND` и `COORDINATION_REDIS_URL` для durable Redis;
- `FIELD_ENCRYPTION_KEYS` либо `FIELD_ENCRYPTION_KEY` с валидным Fernet-ключом;
- `YC_S3_BUCKET`, `YC_S3_ACCESS_KEY` и `YC_S3_SECRET_KEY` для production media;
- явный `BILLING_ENABLED=true|false` и `BILLING_RETURN_URL_ALLOWED_ORIGINS`;
  при включённом billing обязательны `YOOKASSA_SHOP_ID` и
  `YOOKASSA_SECRET_KEY`. Production принимает только HTTPS origins, фиксированный
  `https://api.yookassa.ru/v3` и `YOOKASSA_ALLOW_TEST_PAYMENTS=false`;
- `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`, HTTPS
  `SITE_URL` и `FRONTEND_URL`;
- `RESEND_API_KEY` с domain-scoped Sending access, валидный plain-email
  `DEFAULT_FROM_EMAIL` на `notify.dodugir.com` и
  `EMAIL_HTTP_PROXY_URL=http://egress_proxy:3128`;
- `PUBLIC_HTTP_PROXY_URL=http://egress_proxy:3128`; другой public HTTP proxy в
  production запрещён;
- `PUBLIC_HTTP_PREFLIGHT_URL` зафиксирован в коде на независимом корне Yandex
  Object Storage: check не обращается к bucket/object и остаётся доступен для
  forward recovery при остановленном ingress приложения;
- отдельный `.backup.env` с read-only DB role, S3 credentials без права удаления,
  публичными `BACKUP_AGE_RECIPIENTS` и Ed25519 signing key; private age identity
  на production отсутствует.
- отдельный recovery-only `restore.env` с временными S3 read-only credentials,
  trusted Ed25519 public key и age identity; `.backup.env` для restore запрещён.

Генерация значений без сохранения в shell history:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Секреты должны храниться в secret manager или защищённом `.env` на сервере. `.env`
не коммитится в Git.

Полный bootstrap production host, текущий доменный/TLS-контракт и согласованный
набор значений описаны в [`DEPLOYMENT.md`](DEPLOYMENT.md).

## Redis и миграция Celery broker

Production использует два физических Redis-процесса:

- `redis` — только Django cache, `allkeys-lru`, данные могут вытесняться;
- `redis_broker` — Celery broker (DB 0), result backend (DB 1) и coordination
  locks (DB 2), AOF `everysec`, volume и `noeviction`.

Разные logical DB одного процесса не изолируют eviction и persistence, поэтому
cache запрещено направлять на endpoint broker-а. Production settings проверяют
схему, endpoint-ы и уникальность DB до запуска приложения. Они также сравнивают
URL-decoded credentials с фактическими `CACHE_REDIS_PASSWORD`/
`CELERY_REDIS_PASSWORD`, переданными Redis-процессам, и требуют разные raw
password. Несогласованная пара останавливает pre-deploy до drain/миграций.

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
   secrets, включая подключения web-search, будут перешифрованы первым ключом.
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

Тело ответа получателя не читается: доставка использует только HTTP status и
закрывает соединение. Поэтому уже принятый получателем `2xx` не превращается в
повторную доставку из-за большого, медленного или бесконечного response body.

## Resource limits и исходящие URL

Значения ниже можно уменьшать через environment для более строгой политики.
Увеличение выше встроенного hard ceiling намеренно ограничивается кодом, чтобы
ошибка конфигурации не отключила защиту памяти, времени или кардинальности.

- `MAX_IMAGE_UPLOAD_BYTES` ограничивает ручные и удалённые исходные изображения;
- `MEDIA_PROVIDER_OUTPUT_MAX_BYTES` ограничивает результат media-провайдера;
- `MAX_DECODED_IMAGE_PIXELS` блокирует decompression bombs до полной декодировки;
- `API_BULK_MAX_ITEMS` ограничивает синхронные массовые операции;
- `API_REQUEST_MAX_BYTES` ограничивает non-file request body в Django, а nginx
  отклоняет общий API request крупнее 12 MiB до передачи приложению;
- `FILE_UPLOAD_MEMORY_MAX_BYTES` переводит крупные upload во временный файл,
  вместо удержания всего содержимого в памяти процесса;
- `PART_PAGE_MAX_BYTES` ограничивает HTML-ответы внешних каталогов запчастей;
- `TRUSTED_API_RESPONSE_MAX_BYTES` ограничивает несжатые JSON/text-ответы
  доверенных API (AI, Brave, Tavily и Telegram). Такие запросы используют
  streaming, запрашивают только `identity` encoding и всегда закрывают response;
- `AVITO_API_RESPONSE_MAX_BYTES` отдельно ограничивает ответы фиксированного
  API Avito тем же механизмом;
- `DATASOURCE_UPLOAD_MAX_BYTES` ограничивает фактический объём CSV/XLS/XLSX;
- `DATASOURCE_XLSX_MAX_UNCOMPRESSED_BYTES` блокирует XLSX ZIP bombs до
  передачи архива в `openpyxl`;
- `DATASOURCE_IMPORT_MAX_ROWS` и `DATASOURCE_IMPORT_MAX_CELLS` ограничивают
  физические строки (включая заголовки/пустые строки) и суммарное число ячеек;
- `DATASOURCE_XML_MAX_BYTES` ограничивает распакованное HTTP-клиентом тело XML
  выгрузки 1С;
- `DATASOURCE_HTTP_MAX_BYTES` ограничивает JSON-ответ HTTP-интеграции 1С;
- Все внешние HTTP(S) URL разрешаются приложением на каждом redirect-hop, и DNS
  ответ с любым loopback/private/metadata адресом отклоняется целиком. В
  development соединение открывается к зафиксированному публичному IP. В
  production запрос с исходным hostname передаётся только через фиксированный
  `PUBLIC_HTTP_PROXY_URL=http://egress_proxy:3128`, а Squid независимо разрешает
  hostname и повторно блокирует непубличный итоговый IP через `dst` ACL;
- системные proxy (`trust_env`), произвольный proxy, автоматические retry и
  redirect отключены.
  Redirect обрабатывается вручную, новый URL заново разрешается и фиксируется,
  а credentials и чувствительные заголовки не переходят на другой origin;
- тот же DNS-pinned транспорт и лимиты ответа применяются к изображениям,
  media-provider output, HTML внешних каталогов и HTTP/XML-интеграциям 1С;
- media-provider output принимается только по HTTPS; redirect разрешён только
  внутри исходного origin, а transport error не сохраняет signed URL/query;
- webhook delivery и тест endpoint-а запрещают redirect и не читают тело ответа.

Один абсолютный wall-clock deadline охватывает DNS, admission в ограниченный пул,
connect/TLS, ожидание headers, redirects и body. Одновременно может выполняться не
более 32 защищённых blocking HTTP-операций; при исчерпании слотов новые запросы
завершаются fail-closed в пределах собственного deadline. Это ограничивает ущерб
от зависшего системного resolver, который CPython не умеет безопасно прервать.

CSV и Excel читаются построчно; заявленный клиентом размер файла не считается
доверенным и повторно проверяется по полученным chunk-ам. Для XML-выгрузки 1С
разрешены только публичные HTTP(S) URL. Каждый redirect проходит повторную
DNS/IP-проверку, redirect на другой origin запрещён, чтобы не раскрыть Basic Auth.
XML разбирается без DTD, внешних entities, сети и режима `huge_tree`.

Production Compose использует единственный активный конфиг `nginx.conf` в корне
репозитория. В нём отдельно ограничены billing webhook, checkout/AI top-up,
admin и общий API; устаревший дублирующий конфиг удалён.

YooKassa `return_url` принимается только для origin из
`BILLING_RETURN_URL_ALLOWED_ORIGINS`; в production разрешены только HTTPS origins.
Авторитетный объект Payment обязан содержать `test=false`; тестовые платежи не
активируют подписку/AI-баланс, а `YOOKASSA_ALLOW_TEST_PAYMENTS` запрещён production-настройками.
При `BILLING_ENABLED=true` production требует
`YOOKASSA_SHOP_ID`/`YOOKASSA_SECRET_KEY`, фиксирует API на
`https://api.yookassa.ru/v3`, запрещает сжатые ответы и ограничивает JSON 4 MiB.
При `BILLING_ENABLED=false` checkout, AI top-up, webhook, provider client и
reconciliation закрыты до обращения к БД или сети; read-only billing endpoints
остаются доступны. Deploy по-прежнему проверяет public HTTPS transport
неаутентифицированным side-effect-free GET.
Создание Payment/Refund и авторитетные GET выполняются единым прямым HTTP-клиентом:
SDK YooKassa не используется, redirects отключены, Basic Auth передаётся только на
фиксированный HTTPS origin, заданы connect/read timeouts и `Accept-Encoding: identity`.
Checkout сначала сохраняет неизменяемый Invoice intent и provider idempotency key,
а ошибки webhook и незавершённые платежи сверяются задачей
`reconcile_yookassa_billing` каждые пять минут с ограниченным backoff. Ручной
запуск для диагностики: `python manage.py reconcile_yookassa --invoice-id ID --force`.

Checkout-endpoint-ы явно выполняются вне `ATOMIC_REQUESTS`: сетевой запрос к
YooKassa начинается только после commit устойчивого Invoice intent. Сервис также
останавливает checkout при попытке вызвать его из внешнего `transaction.atomic()`.
Одновременно активные intents с одинаковым tenant и payload дедуплицируются под
lock подписки и защищены частичным уникальным constraint в PostgreSQL. Поэтому
разные client UUID из параллельных вкладок используют один provider payment, а
каждый принятый UUID неизменно связывается с canonical Invoice в
`CheckoutIntentKey`. Потеря ответа во второй вкладке поэтому не может превратить
её следующий retry в новый платёж.
Число ключей на Invoice ограничено `BILLING_CHECKOUT_MAX_KEYS_PER_INVOICE`;
слот canonical key резервируется даже при падении между commit Invoice и созданием
registry-записи. Новый alias сверх лимита получает `409 checkout_key_limit`, а уже
принятые ключи сохраняют идемпотентность.
Повтор с ключом уже завершённого Invoice (`paid`, `failed`,
`partially_refunded`, `refunded`) не возвращает устаревший confirmation URL: API отвечает
`409 checkout_terminal` с `rotate_idempotency_key=true`. Только этот код разрешает
frontend удалить сохранённый ключ и создать новый. `checkout_pending` и
`checkout_manual_review`, произвольные `4xx/5xx`, timeout и network error ключ не
освобождают.

Для subscription checkout действует дополнительный финансовый барьер. У tenant
может существовать только один незавершённый subscription intent независимо от
plan/period; конкурирующий payload получает
`409 subscription_checkout_in_progress` до второго provider request. Пока
proration/credit явно не реализованы, изменение или продление действующей
оплаченной подписки отклоняется кодом
`409 active_subscription_change_not_supported` до создания Invoice. Это не даёт
сжечь остаток уже оплаченного периода при немедленной смене плана. Trial conversion
и восстановление billing-only подписки разрешены; новый месяц/год добавляется от
`max(today, current_period_end)`, поэтому уже обещанные дни не теряются.

Уведомления об оплате, истечении подписки и порогах AI-баланса, а также повторная
постановка листингов после снятия лимита сохраняются в `BillingOutboxEvent` внутри
той же транзакции, что и финансовое изменение. Немедленный запуск dispatcher —
только ускорение:
`dispatch_billing_outbox` каждую минуту повторно подбирает pending и просроченные
processing-события. Broker errors получают экспоненциальный backoff без удаления
события. После `BILLING_OUTBOX_MAX_ATTEMPTS` событие переводится в `dead`, больше
автоматически не выбирается и остаётся доступным в read-only billing admin.
После устранения причины оператор выполняет адресный принудительный повтор:

```bash
python manage.py dispatch_billing_outbox --limit 100
python manage.py dispatch_billing_outbox --event-id UUID --force
```

`--force` без явного `--event-id` отклоняется, чтобы операторская команда не могла
массово обойти backoff всех ожидающих событий.

Публикация outbox имеет семантику at-least-once: событие не теряется при падении
broker, но авария процесса между подтверждением broker и DB-finalize теоретически
может повторить downstream-задачу. Для трассировки используется стабильный Celery
`task_id`; финансовые проводки и entitlement остаются идемпотентными независимо от
повторной публикации. Настройки retry: `BILLING_OUTBOX_BASE_DELAY_SECONDS`,
`BILLING_OUTBOX_MAX_DELAY_SECONDS`, `BILLING_OUTBOX_PROCESSING_TIMEOUT_SECONDS` и
`BILLING_OUTBOX_BATCH_SIZE`, `BILLING_OUTBOX_MAX_ATTEMPTS`. Только успешно
опубликованные outbox-записи удаляются
общим retention-процессом через `BILLING_AUDIT_RETENTION_DAYS`; pending/processing
записи retention не затрагивает.

Public auth endpoints имеют раздельные application-level лимиты по адресу
клиента и по SHA-256 fingerprint нормализованного email. Токены восстановления
пароля и смены email передаются frontend-у только в URL fragment, удаляются из
адресной строки до рендера формы и отправляются backend-у через POST.

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
endpoint-ы, необходимые Avito, AI-провайдерам, S3 и tenant webhook-ам. Public URL
transport принимает только точный `PUBLIC_HTTP_PROXY_URL`; application DNS
admission и Squid `dst` ACL являются независимыми проверками одного назначения.
`check_public_http_connectivity` выполняет только GET несуществующего YooKassa
payment sentinel и до drain проверяет этот маршрут, TLS и credentials без записи.

SMTP является отдельным узким исключением: Squid принимает CONNECT на порт 587
только для точного имени `smtp.resend.com`. Django не использует прямой
`smtplib` socket наружу, а открывает CONNECT tunnel через внутренний proxy;
SMTP greeting, STARTTLS, проверка upstream-сертификата и login происходят внутри
туннеля. `check_email_connectivity` не отправляет письмо и скрывает текст provider
errors, но перед drain доказывает доступность маршрута и credentials из нового
image. Изменение host/порта/proxy требует отдельного threat review и синхронного
обновления backend, Squid ACL, Compose contract и тестов.

Глобальный SMTP credential и `DEFAULT_FROM_EMAIL` обслуживают только письма
платформы (восстановление пароля, подтверждение email, security notifications).
Отправка от имени тенанта требует отдельной проверенной sender identity. Для
tenant mail используется domain-scoped credential или BYOK с отдельными quotas,
suppression/audit trail и webhook routing; произвольный tenant `From` через
platform credential запрещён.

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
Ручной incident deploy обязан сохранить текущий `git rev-parse HEAD` до checkout
target и передать его как `PREVIOUS_SHA`; скрипт fail-close отклоняет запуск без
этого значения.

На сервере скопируйте `.deploy.env.example` в `.deploy.env`, направьте
`PROD_SMOKE_URL` на публичный `/api/v1/ready/` и ограничьте доступ к файлу
(`chmod 600`). Файл должен быть обычным, не symlink, принадлежать deploy
user и содержать только разрешённые уникальные `KEY=value` без shell-
синтаксиса. `/api/v1/live/` проверяет только HTTP-процесс, а readiness также
проверяет PostgreSQL и cache. `PROD_DRAIN_TIMEOUT_SECONDS` должен быть больше
наибольшего hard time limit фоновой задачи (по умолчанию 3700 секунд при лимите
3600 секунд). Скрипт:

1. блокирует параллельный ручной запуск и проверяет SHA, чистоту working tree,
   Compose-конфигурацию, доступность Docker и запас диска;
2. сохраняет image ID текущего release и собирает новый до переключения web/worker
   процессов;
3. выполняет `check --deploy`, migration plan и bounded connectivity gates для
   Redis, SMTP и public HTTPS/YooKassa, затем включает maintenance: останавливает
   ingress, beat, старый web и workers с graceful drain;
4. при остановленных writers делает обязательный зашифрованный S3 backup и только
   после его успеха применяет миграции в one-shot контейнере;
5. готовит release data, запускает новый release, ждёт readiness всех сервисов и
   проверяет публичный HTTPS endpoint;
6. при ошибке **до** начала миграций возвращает application-сервисы на предыдущие
   сохранённые образы.

### Maintenance и forward recovery

Окно недоступности начинается с остановки `nginx` и заканчивается успешным внешним
smoke-check. В него входят graceful drain, backup, миграции, подготовка данных и
readiness. Перед deploy предупредите пользователей об окне и убедитесь, что нет
длительных импортов/экспортов. Не уменьшайте drain timeout ниже максимального
времени задачи: принудительно завершённый worker мог уже выполнить внешний side
effect, но ещё не зафиксировать локальный результат.

После установки `MIGRATIONS_STARTED=true` автоматический rollback намеренно
запрещён. Старый web/worker release остаётся остановленным: даже частично
применённая схема может быть с ним несовместима. Дежурный оператор должен:

1. оставить `nginx`, `django`, `frontend`, `celery_beat`, `celery_worker` и
   `celery_worker_images` остановленными;
2. сохранить вывод упавшей команды и проверить таблицу `django_migrations`,
   migration plan и наличие успешного pre-migration backup/manifest;
3. подготовить через обычный CI новый forward-fix (или исправленную идемпотентную
   data migration), пройти review и развернуть точный проверенный SHA;
4. повторить deploy: новый backup создаётся снова, `migrate` продолжает историю,
   а writers запускаются только после завершения всех миграций;
5. выполнить readiness/smoke-check и предметную сверку billing/webhook/outbox до
   завершения maintenance window.

Не применяйте обратные Django-миграции и не запускайте старый release вручную без
отдельно проверенного restore/cutover-плана. Если forward recovery невозможен,
используйте процедуру восстановления из зашифрованного backup на отдельном хосте
из [`BACKUP_RESTORE.md`](BACKUP_RESTORE.md), затем выполняйте контролируемый
cutover.

Глобальные `docker * prune` намеренно не выполняются: один deploy не должен удалять
ресурсы других Compose-проектов на том же хосте. Миграции БД автоматически не
откатываются, поэтому production-миграции должны следовать expand/contract-подходу.
Ошибка pre-deploy backup блокирует миграцию. Полная настройка, retention и
ежемесячный restore drill:
[`BACKUP_RESTORE.md`](BACKUP_RESTORE.md).
