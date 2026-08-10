# Production deployment

## Назначение и текущий контракт

Этот runbook описывает текущий production-контур MAP. Он не является универсальным
шаблоном для произвольного домена:

- checkout находится в `/opt/saas_poster`;
- публичный адрес — `https://dodugir.com`, дополнительное имя —
  `www.dodugir.com`;
- HTTP перенаправляется на канонический `https://dodugir.com`;
- Nginx читает уже выпущенный сертификат из
  `/etc/letsencrypt/live/dodugir.com` и
  `/etc/letsencrypt/archive/dodugir.com`;
- production запускается через `docker-compose.prod.yml` и Docker Compose v2;
- автоматический deploy принимает точный 40-символьный commit SHA, успешно
  прошедший CI для `main`.

CI собирает и сканирует production images, однако текущий deploy не переносит эти
images из registry: `deploy.sh` повторно собирает их на production host из того же
проверенного SHA. Исходники, Python/npm lock-файлы, base-image digests и npm tarball
checksum зафиксированы, но это пока не схема «build once, deploy exact image digest».

## 1. Подготовка production host

До первого deploy:

1. Направьте DNS `dodugir.com` и `www.dodugir.com` на production host.
2. Разрешите входящий трафик только на необходимые порты: публичные `80/443` и
   административный SSH из доверенных сетей. PostgreSQL и Redis наружу не
   публикуются production Compose-файлом.
3. Установите Git, Docker Engine, Docker Compose v2, `curl`, `flock`, `df`, `awk`,
   `stat` и GNU `timeout`. Стандартный deploy/backup operator — `saas-poster`: он
   должен владеть checkout вместе с `.git`, иметь право записи для fetch/checkout
   и rollback, а также доступ к Docker daemon.
4. Создайте checkout `/opt/saas_poster` с remote `origin`, указывающим на этот
   репозиторий. Рабочая копия на сервере не должна содержать ни tracked,
   ни untracked-изменений: `Dockerfile` копирует checkout в image, поэтому
   любой Git drift нарушает соответствие `TARGET_SHA`.
5. Выпустите TLS-сертификат и включите timer автоматического продления до запуска
   production Nginx. Reload deploy-hook устанавливается и проверяется после
   первого успешного старта Nginx по разделу 6. Оба каталога ниже должны
   существовать и быть доступны Docker daemon, а файлы сертификата — читаться из
   bind mount:

   ```bash
   sudo test -r /etc/letsencrypt/live/dodugir.com/fullchain.pem
   sudo test -r /etc/letsencrypt/live/dodugir.com/privkey.pem
   sudo test -d /etc/letsencrypt/archive/dodugir.com
   ```
6. Создайте принадлежащий deploy user каталог блокировок с mode `0700`.
   `deploy.sh` намеренно не использует предсказуемый файл в `/tmp` и завершится
   до любых изменений runtime, если каталог отсутствует, является symlink или
   принадлежит другому пользователю. Для стандартного пользователя:

   ```bash
   sudo install -d -o saas-poster -g saas-poster -m 0700 /run/lock/saas-poster
   ```

   `/run` очищается после reboot. Чтобы каталог создавался автоматически,
   добавьте управляемый конфигурацией хоста файл
   `/etc/tmpfiles.d/saas-poster.conf` (замените пользователя и группу на
   фактические):

   ```text
   d /run/lock/saas-poster 0700 saas-poster saas-poster -
   ```

   Затем примените его один раз:

   ```bash
   sudo systemd-tmpfiles --create /etc/tmpfiles.d/saas-poster.conf
   ```

Не запускайте `deploy.sh`, пока DNS, сертификат, application secrets и backup
контур не подготовлены: миграции разрешаются только после успешного encrypted
backup.

## 2. Application `.env`

Production-файл `/opt/saas_poster/.env` создаётся через secret manager или
защищённый editor и имеет mode `600` или `400`. Локальный `.env.example` содержит
development defaults (`map_password`, localhost origins и `MEDIA_KEY_PREFIX=dev`),
поэтому его нельзя копировать в production без полной замены значений.

Минимальный согласованный набор для текущего домена:

```dotenv
DJANGO_SECRET_KEY=<random-at-least-50-characters>
ALLOWED_HOSTS=dodugir.com,www.dodugir.com
CSRF_TRUSTED_ORIGINS=https://dodugir.com,https://www.dodugir.com
CORS_ALLOWED_ORIGINS=https://dodugir.com,https://www.dodugir.com

POSTGRES_DB=<production-database-name>
POSTGRES_USER=<production-database-user>
POSTGRES_PASSWORD=<random-database-password>
DATABASE_URL=postgresql://<user>:<password>@db:5432/<database>

CACHE_REDIS_PASSWORD=<random-cache-password>
CELERY_REDIS_PASSWORD=<different-random-broker-password>
CACHE_REDIS_URL=redis://:<cache-password>@redis:6379/0
CELERY_BROKER_URL=redis://:<broker-password>@redis_broker:6379/0
CELERY_RESULT_BACKEND=redis://:<broker-password>@redis_broker:6379/1
COORDINATION_REDIS_URL=redis://:<broker-password>@redis_broker:6379/2

FIELD_ENCRYPTION_KEYS=<primary-fernet-key>[,<previous-fernet-key>]

YC_S3_BUCKET=<production-public-media-bucket>
YC_S3_ACCESS_KEY=<media-writer-access-key>
YC_S3_SECRET_KEY=<media-writer-secret-key>
YC_CDN_DOMAIN=<optional-public-cdn-domain>
MEDIA_KEY_PREFIX=prod

SITE_URL=https://dodugir.com
FRONTEND_URL=https://dodugir.com
BILLING_RETURN_URL_ALLOWED_ORIGINS=https://dodugir.com
BILLING_ENABLED=true
YOOKASSA_SHOP_ID=<production-shop-id>
YOOKASSA_SECRET_KEY=<production-secret-key>
YOOKASSA_API_BASE_URL=https://api.yookassa.ru/v3
YOOKASSA_ALLOW_TEST_PAYMENTS=false
PUBLIC_HTTP_PROXY_URL=http://egress_proxy:3128

RESEND_API_KEY=<domain-scoped-production-sending-key>
DEFAULT_FROM_EMAIL=noreply@notify.dodugir.com
EMAIL_HTTP_PROXY_URL=http://egress_proxy:3128
```

Значения в угловых скобках — placeholders, их нельзя оставлять буквально.
Если CDN не используется, оставьте `YC_CDN_DOMAIN` пустым.
До подключения платёжного провайдера явно задайте `BILLING_ENABLED=false` и
оставьте `YOOKASSA_SHOP_ID`/`YOOKASSA_SECRET_KEY` пустыми. В этом режиме чтение
тарифов, подписки, лимитов и истории доступно, а checkout, AI top-up, webhook,
provider API и reconciliation закрыты fail-closed. Для включения оплаты одной
замены флага недостаточно: сначала добавьте production credentials и webhook,
затем выполните полный deploy с provider preflight.

Пароли в URL должны быть URL-safe либо percent-encoded. Cache и durable broker
обязаны быть разными Redis-процессами; DB `0/1/2` broker-процесса зарезервированы
для broker, result backend и coordination соответственно. После URL-decoding
пароль `CACHE_REDIS_URL` обязан точно совпадать с `CACHE_REDIS_PASSWORD`, а пароли
в трёх durable URL — с `CELERY_REDIS_PASSWORD`; эти два raw password должны быть
разными. Production settings проверяют это до maintenance/drain. Полные
ограничения и ротация Fernet-ключей описаны в
[`PRODUCTION_SECURITY.md`](PRODUCTION_SECURITY.md).

Media bucket предназначен для публично читаемых изображений товаров. Не храните
в default storage приватные документы или секреты.

Application-контейнеры не имеют прямого SMTP-маршрута. Django открывает туннель
через фиксированный `egress_proxy:3128` только к
`smtp.resend.com:587`, затем выполняет STARTTLS с проверкой сертификата и
SMTP login. Resend key должен иметь только Sending access и быть ограничен
доменом `notify.dodugir.com`; `DEFAULT_FROM_EMAIL` также обязан принадлежать
этому platform-домену. Другой SMTP host, proxy URL, пустой/невалидный key или
чужой sender domain останавливают production settings. Deploy дополнительно
проверяет CONNECT, greeting, STARTTLS и login из нового image без отправки письма.

Этот SMTP channel предназначен только для security/transactional писем самой
платформы. Будущие письма от имени тенантов должны использовать отдельные
проверенные tenant sender identities и отдельные domain-scoped/BYOK credentials;
tenant-настройки не могут переопределять `DEFAULT_FROM_EMAIL`.

### Первичная регистрация YooKassa webhook

До установки `BILLING_ENABLED=true` зарегистрируйте в кабинете/API YooKassa точный
HTTPS endpoint:

```text
https://dodugir.com/api/v1/billing/webhook/yookassa/
```

Подпишите endpoint минимум на события `payment.succeeded`, `payment.canceled` и
`refund.succeeded`. Endpoint публичный, а приложение допускает запросы только с
разрешённых адресов YooKassa и перепроверяет финансовое состояние через API
провайдера. После регистрации подтвердите в кабинете YooKassa успешную доставку
каждого типа события и отсутствие rejected/retry backlog; не имитируйте
production-платёж произвольным локальным `curl` payload.

## 3. Backup и deploy-конфигурация

Создайте `/opt/saas_poster/.backup.env` из `.backup.env.example`, заполните его
отдельными DB/S3 credentials и signing keys, затем настройте bucket versioning,
lifecycle и restore drill по
[`BACKUP_RESTORE.md`](BACKUP_RESTORE.md). Private age identity на production host
не размещается.

Создайте `/opt/saas_poster/.deploy.env` из `.deploy.env.example`:

```dotenv
PROD_SMOKE_URL=https://dodugir.com/api/v1/ready/
PROD_MIN_FREE_DISK_MB=2048
PROD_HEALTH_RETRIES=40
PROD_HEALTH_INTERVAL_SECONDS=3
PROD_LOG_TAIL=200
PROD_ROLLBACK_ENABLED=true
PROD_BACKUP_TIMEOUT_SECONDS=7200
PROD_DRAIN_TIMEOUT_SECONDS=3700
PROD_BROKER_MIGRATION_CONFIRMED=false
```

`.deploy.env` читается не как shell-скрипт, а строгим allowlist-parser:
только перечисленные в шаблоне ключи и только один раз, в формате
`KEY=value` без `export`, кавычек, пробелов и shell-подстановок. Неизвестные
ключи, дубликаты и некорректные строки останавливают deploy до любых
runtime-изменений.

Защитите все три файла. Каждый должен быть обычным файлом, а не
symlink, принадлежать deploy user и иметь mode `600` или `400`:

```bash
chmod 600 /opt/saas_poster/.env \
  /opt/saas_poster/.backup.env \
  /opt/saas_poster/.deploy.env
```

`PROD_BROKER_MIGRATION_CONFIRMED=true` ставится только после документированного
drain legacy Celery queues. Для чистой установки без legacy broker оператор всё
равно должен подтвердить, что переносить нечего. Порядок миграции broker описан в
[`PRODUCTION_SECURITY.md`](PRODUCTION_SECURITY.md#redis-и-миграция-celery-broker).

## 4. GitHub environment

Создайте protected environment `production`, включите required reviewers и
настройте:

- repository variable `PROD_DEPLOY_ENABLED=true`;
- secrets `PROD_HOST`, `PROD_USER`, `PROD_SSH_KEY`;
- secret `PROD_HOST_FINGERPRINT` в формате `SHA256:...`.

Fingerprint берётся по доверенному административному каналу, а не из результата
того же `ssh-keyscan`, который затем проверяется. Deploy workflow сверяет
полученный host key с этим fingerprint и использует отдельный временный
`known_hosts`.

## 5. Автоматический deploy

Канонический путь — успешный `CI` для `push` в `main`, после которого workflow
`Deploy`:

1. получает `workflow_run.head_sha` и проверяет, что это полный SHA успешного
   `push` в `main`;
2. подключается по SSH с проверкой host fingerprint;
3. на сервере выполняет `git fetch --no-tags origin main` и пропускает устаревший
   deploy, если `main` уже указывает на другой commit;
4. переключает checkout в detached HEAD на точный SHA и передаёт его в
   `deploy.sh`.

`deploy.sh` затем:

1. блокирует параллельный запуск, проверяет SHA, полное отсутствие tracked/
   untracked Git drift, mode/type/owner secret-файлов, Compose config, Docker и
   свободное место;
2. сохраняет image IDs текущего release и собирает новый release до остановки
   application services;
3. выполняет `check --deploy`, проверку незаписанных миграций, `migrate --plan`,
   ограниченный по времени Redis `PING` для cache, broker, result backend и
   coordination store, SMTP CONNECT/STARTTLS/login и side-effect-free public
   HTTPS GET к YooKassa из нового Django image; все connectivity gates
   завершаются до maintenance/drain;
4. останавливает ingress, Beat, web и workers с graceful drain;
5. создаёт обязательный зашифрованный и подписанный S3 backup;
6. только после успешного backup применяет миграции;
7. собирает static data, обновляет periodic tasks и tenant categories;
8. запускает release, ждёт healthchecks всех сервисов и проверяет публичный
   `PROD_SMOKE_URL`.

Ручной запуск `deploy.sh` не является способом обойти CI или protected
environment: он допустим только по отдельной incident/change процедуре с тем же
проверенным SHA и зафиксированным предыдущим SHA.

## 6. Проверка после deploy

Успех deploy подтверждается одновременно:

- healthy-состоянием `db`, обоих Redis, egress proxy, Django, обоих workers,
  Beat, frontend и Nginx;
- успешным `https://dodugir.com/api/v1/ready/`;
- отсутствием failed backup/freshness units;
- предметным smoke-тестом login, чтения каталога и billing/webhook outbox после
  изменений соответствующего контура; после изменения почтовых credentials или
  backend — доставкой тестового сообщения на контролируемый адрес.

### Автоматическое продление TLS

После первого успешного запуска Nginx установите executable deploy-hook Certbot,
который сначала валидирует конфигурацию внутри production-контейнера, а затем
делает graceful reload:

```bash
sudo ln -sfn /opt/saas_poster/scripts/reload_production_nginx.sh \
  /etc/letsencrypt/renewal-hooks/deploy/saas-poster-nginx-reload
sudo test -x /etc/letsencrypt/renewal-hooks/deploy/saas-poster-nginx-reload
sudo certbot renew --dry-run --run-deploy-hooks
```

Dry-run должен завершиться успешно при работающем production Nginx. Дополнительно
проверьте с внешнего узла, что `https://dodugir.com` отдаёт ожидаемый актуальный
сертификат. После изменения checkout path обновите symlink и задайте в скрипте
новый фиксированный абсолютный `ROOT_DIR` отдельным reviewed изменением. Hook
намеренно не принимает checkout path из окружения, так как Certbot запускает его
с повышенными привилегиями.

`/api/v1/live/` подтверждает только жизнь HTTP-процесса. Readiness дополнительно
проверяет PostgreSQL и cache, но не заменяет бизнес smoke-тест.

## 7. Ошибка и восстановление

- До начала миграций deploy может вернуть application services на сохранённые
  images предыдущего SHA, если `PROD_ROLLBACK_ENABLED=true`.
- После установки `MIGRATIONS_STARTED=true` автоматический rollback запрещён.
  Старый release нельзя запускать поверх потенциально изменённой схемы.
- При ошибке миграции оставьте ingress, web, Beat и workers остановленными,
  сохраните логи, проверьте `django_migrations` и pre-migration backup, затем
  готовьте forward-fix через обычный CI.
- Если forward recovery невозможен, восстановите backup в новую пустую БД по
  [`BACKUP_RESTORE.md`](BACKUP_RESTORE.md) и выполните отдельный контролируемый
  cutover.

Глобальные `docker * prune`, удаление volumes, обратные Django-миграции и
production restore «поверх» текущей БД в deploy-процедуру не входят.

## 8. Смена домена

Смена `dodugir.com` — инфраструктурное изменение, а не одна env-переменная. В
одном reviewed change необходимо:

1. обновить DNS и заранее выпустить сертификат нового домена;
2. изменить `server_name`, HTTP redirect и пути `ssl_certificate`/
   `ssl_certificate_key` в `nginx.conf`;
3. изменить оба certificate bind mount в `docker-compose.prod.yml`;
4. синхронно обновить `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`,
   `CORS_ALLOWED_ORIGINS`, `SITE_URL`, `FRONTEND_URL`,
   `BILLING_RETURN_URL_ALLOWED_ORIGINS` и `PROD_SMOKE_URL`;
5. обновить return/webhook URL в YooKassa и других внешних интеграциях;
6. проверить certificate renewal, Nginx config, Django `check --deploy`, frontend
   same-origin `/api`, readiness и внешний HTTPS smoke-test;
7. только после проверки нового домена менять redirect и удалять старый
   сертификат/имя из конфигурации.

Не меняйте только `.env`: при старых Nginx mounts контейнер либо не запустится,
либо продолжит обслуживать прежний сертификат и redirect.
