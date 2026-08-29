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

### Текущий production-контракт фидов Avito

P0–P6 внедрены; фактическое состояние и release evidence зафиксированы в
[`AVITO_FEED_STATUS.md`](AVITO_FEED_STATUS.md), границы будущего P7 — в
[`AVITO_FEED_ROADMAP.md`](AVITO_FEED_ROADMAP.md).

Каждый production deploy обязан сохранить точную согласованную комбинацию:

```text
AVITO_STATUS_LIFECYCLE_MODE=dual_write
MARKETPLACE_FEED_RUN_MODE=durable
MARKETPLACE_FEED_INGRESS_MODE=dual_write
MARKETPLACE_FEED_ARTIFACT_MODE=active
MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS=
MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false
MARKETPLACE_FEED_STORAGE_MODE=stable_bridge
```

Пустой allowlist означает fleet-default. Допустимый минимальный admission
rollback — одной операцией вернуть `run=legacy` и exact allowlist проверенного
account `4`; смешивать пустой allowlist с `legacy` или один ID с `durable`
нельзя. Artifact/VersionId/evidence при rollback не удаляются. Более глубокий
откат выполняется только по
[`P6_PRIVATE_FEED_CANARY_RUNBOOK.md`](P6_PRIVATE_FEED_CANARY_RUNBOOK.md).
P7, GC, cleanup, object deletion и `0039` остаются заморожены.

## 1. Подготовка production host

До первого deploy:

1. Направьте DNS `dodugir.com` и `www.dodugir.com` на production host.
2. Разрешите входящий трафик только на необходимые порты: публичные `80/443` и
   административный SSH из доверенных сетей. PostgreSQL и Redis наружу не
   публикуются production Compose-файлом.
3. Установите Git, Docker Engine, Docker Compose v2, `curl`, `flock`, `df`, `awk`,
   `stat` и GNU `timeout`. Canonical checkout, `.git`, secret-файлы и Docker
   operations принадлежат `root`. GitHub подключается отдельным public-key-only
   пользователем `mapdeploy`: его ключ имеет SSH `ForcedCommand`, а sudoers
   разрешает только проверенный release entrypoint, backup freshness и topology
   check. Не добавляйте `mapdeploy` в группу `docker` и не давайте ему доступ к
   checkout или secret-файлам.
   Поддерживаемый минимальный размер текущего single-host контура — 2 vCPU и
   3584 MiB RAM. Compose ограничивает обычный runtime суммарно 2816 MiB, а
   одноразовый backup — ещё 384 MiB, оставляя память ядру/Docker. Уменьшать
   host ниже этого tier или повышать отдельные `mem_limit` можно только вместе
   с пересчётом aggregate budget и нагрузочным тестом. Deploy собирает образы
   последовательно (`COMPOSE_PARALLEL_LIMIT=1`), чтобы не создавать параллельный
   build peak на небольшом host.
   Pre-deploy capacity gate требует не менее 1024 MiB `MemAvailable`
   перед сборкой; при меньшем запасе deploy останавливается до
   изменения runtime.
4. Создайте checkout `/opt/saas_poster` с remote `origin`, указывающим на этот
   репозиторий. Рабочая копия на сервере не должна содержать ни tracked,
   ни untracked-изменений: `Dockerfile` копирует checkout в image, поэтому
   любой Git drift нарушает соответствие `TARGET_SHA`. Сам `/opt`, checkout,
   `.git` и каждый путь внутри checkout должны принадлежать `root`, не быть
   symlink и не иметь group/world-write. Установленный root-owned validator
   проверяет это перед release, backup, topology/capacity check и Certbot reload;
   нарушение блокирует операцию до исполнения Compose из checkout.
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
6. Создайте принадлежащий `root` каталог блокировок с mode `0700`.
   `deploy.sh` намеренно не использует предсказуемый файл в `/tmp` и завершится
   до любых изменений runtime, если каталог отсутствует, является symlink или
   принадлежит другому пользователю. Для стандартного пользователя:

   ```bash
   sudo install -d -o root -g root -m 0700 /run/lock/saas-poster
   ```

   `/run` очищается после reboot. Чтобы каталог создавался автоматически,
   добавьте управляемый конфигурацией хоста файл
   `/etc/tmpfiles.d/saas-poster.conf`:

   ```text
   d /run/lock/saas-poster 0700 root root -
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
Каждое реальное platform-письмо содержит стабильный `Resend-Idempotency-Key`;
автоматические повторы ограничены 23 часами, что короче 24-часового окна Resend.

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
PROD_MIN_FREE_DISK_MB=16384
PROD_HEALTH_RETRIES=40
PROD_HEALTH_INTERVAL_SECONDS=3
PROD_LOG_TAIL=200
PROD_ROLLBACK_ENABLED=true
PROD_BACKUP_TIMEOUT_SECONDS=7200
PROD_BEAT_STOP_TIMEOUT_SECONDS=45
PROD_DRAIN_TIMEOUT_SECONDS=3700
PROD_BROKER_MIGRATION_CONFIRMED=false
```

`.deploy.env` читается не как shell-скрипт, а строгим allowlist-parser:
только перечисленные в шаблоне ключи и только один раз, в формате
`KEY=value` без `export`, кавычек, пробелов и shell-подстановок. Неизвестные
ключи, дубликаты и некорректные строки останавливают deploy до любых
runtime-изменений.

Защитите все три файла. Каждый должен быть обычным файлом, а не
symlink, принадлежать `root` и иметь mode `600` или `400`:

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
`known_hosts`. Четыре SSH secrets храните на repository-level: их также
использует неинтерактивный hourly monitor; environment `production` ограничивает
только deploy job.

`PROD_USER` должен быть равен `mapdeploy`, а `PROD_SSH_KEY` — отдельному Ed25519
ключу только этого репозитория. Перед включением workflow установите host
контракт из canonical checkout, передав публичную часть ключа:

```bash
cd /opt/saas_poster
sudo ./scripts/install_production_host_services.sh /secure/path/mapdeploy.pub
sudo -u mapdeploy test ! -r /opt/saas_poster/.env
sudo -u mapdeploy test ! -r /var/run/docker.sock
```

Installer создаёт public-key-only account, forced-command `authorized_keys`,
отдельный `sshd_config.d` Match contract, минимальный sudoers allowlist,
root-owned backup/freshness units, tmpfiles-конфигурацию и Certbot reload hook.
Закрытый ключ не копируется на production host.

Release/gateway, checkout validator, backup/freshness, topology/capacity и
Certbot reload entrypoints устанавливаются root-owned копиями в
`/usr/local/sbin`, а
SSH/sudoers/systemd/tmpfiles — в `/etc`. Поэтому PR, меняющий любую
часть host-контракта, требует повторного bootstrap после зелёного CI, но
до обычного deploy. Нельзя просто оставить checkout на target: тогда release
ошибочно сохранит target как `PREVIOUS_SHA` и лишится rollback. Канонический
bootstrap держит общий release lock, временно проверяет target как
точный `origin/main`, устанавливает контракт и возвращает checkout на
фактически работающий SHA:

```bash
target_sha=0123456789abcdef0123456789abcdef01234567
previous_sha="$(git rev-parse HEAD)"
sudo ./scripts/bootstrap_production_host_contract.sh \
  "$target_sha" /secure/path/mapdeploy.pub
test "$(git rev-parse HEAD)" = "$previous_sha"
```

Bootstrap также отключает backup/freshness timers и ждёт завершения уже
запущенных legacy units до смены checkout. После установки он оставляет
root-only marker точного target SHA; таймеры остаются отключёнными и включаются
только самим успешным deploy после topology/external smoke. Если deploy не
завершился, это намеренное fail-closed состояние: устраните причину и повторите
тот же target, не включая timers вручную на старом checkout.

Для самого первого rollout, когда bootstrap-script ещё нет в текущем
release, администратор извлекает **только этот script** из прошедшего CI
target, передаёт его по защищённому root SSH-каналу в
`/root/bootstrap_production_host_contract.sh`, затем сверяет SHA-256 с
локальным `git show TARGET_SHA:scripts/bootstrap_production_host_contract.sh` и
запускает переданную root-owned копию. Не переключайте production
checkout вручную: script сам проверит target и вернёт previous SHA. Preflight
обычного deploy побайтно проверяет весь установленный host-контракт и
останавливается до Docker-изменений при drift.

## 5. Автоматический deploy

Канонический путь — успешный `CI` для `push` в `main`, после которого workflow
`Deploy`:

1. получает exact-run CI gate artifact и `workflow_run.head_sha`; режим `docs`
   завершает workflow без production deploy, а `full`/`reuse` разрешает
   продолжение только для успешного `push` в `main`;
2. подключается как `mapdeploy` по SSH с проверкой host fingerprint;
3. передаёт forced-command протоколу только `deploy <40-char-sha>`;
4. root-owned release entrypoint выполняет `git fetch --no-tags origin main`,
   пропускает устаревший SHA, проверяет ancestry, сохраняет предыдущий SHA,
   переключает checkout в detached HEAD и запускает `deploy.sh`.

Required check сохраняет имя `test`. Markdown-only изменения в `docs/` и
корневые `*.md` проходят короткий `git diff --check`; любой другой файл
fail-closed включает полный gate. Полный gate параллельно выполняет backend
contracts/schema/supply-chain, три исчерпывающих backend test shards с единым
coverage threshold, frontend и production image/runtime security.

После успешного полного gate CI сохраняет evidence проверенного Git tree. Для
последующего `push` того же дерева полный gate можно не повторять только если
GitHub API подтверждает неистёкший artifact успешного workflow этого же
репозитория. Совпадения commit SHA недостаточно; сравнивается tree SHA. Ошибка
API, foreign/fork run, иной workflow, неуспешный run или другое дерево всегда
возвращают полный CI.

`deploy.sh` затем:

1. блокирует параллельный запуск, проверяет SHA, полное отсутствие tracked/
   untracked Git drift, mode/type/owner secret-файлов, Compose config, Docker и
   свободное место;
2. сохраняет image IDs текущего release и собирает новый release до остановки
   application services;
3. выполняет `check --deploy`, проверку незаписанных миграций, `migrate --plan`,
   ограниченный по времени Redis `PING` для cache, broker, result backend и
   coordination store, SMTP CONNECT/STARTTLS/login и side-effect-free public
   HTTPS GET к независимому корню Yandex Object Storage, а при включённом
   billing — к YooKassa с проверкой credentials, из нового Django image; все
   connectivity gates
   завершаются до maintenance/drain;
4. останавливает ingress, Beat, web и workers с graceful drain;
5. создаёт обязательный зашифрованный и подписанный S3 backup;
6. только после успешного backup применяет миграции;
7. идемпотентно создаёт canonical plans, собирает static data, обновляет periodic
   tasks и tenant categories;
8. запускает release, ждёт healthchecks всех сервисов, проверяет точное сетевое
   членство каждого контейнера и host bindings Nginx, затем проверяет публичный
   `PROD_SMOKE_URL`.

Ручной запуск `deploy.sh` не является способом обойти CI или protected
environment: он допустим только по отдельной incident/change процедуре с тем же
проверенным SHA и зафиксированным предыдущим SHA. `PREVIOUS_SHA` обязателен:
его нужно сохранить **до** checkout целевого commit, иначе автоматический
rollback до начала миграций невозможен.

```bash
sudo -i
cd /opt/saas_poster
previous_sha="$(git rev-parse HEAD)"
git fetch --no-tags origin main
target_sha="$(git rev-parse origin/main)"
git checkout --detach "$target_sha"
PREVIOUS_SHA="$previous_sha" ./deploy.sh "$target_sha"
```

Nginx подключён одновременно к внутренней `backend` и отдельной внешней
`ingress_public`; только Nginx имеет доступ к последней. Если `docker compose ps`
не показывает host bindings `0.0.0.0:80->80` и `0.0.0.0:443->443`, release нельзя
считать доступным, даже если внутренний Nginx healthcheck зелёный.

## 6. Проверка после deploy

Успех deploy подтверждается одновременно:

- healthy-состоянием `db`, обоих Redis, egress proxy, Django, обоих workers,
  Beat, frontend и Nginx;
- успешным `https://dodugir.com/api/v1/ready/`;
- отсутствием failed backup/freshness units;
- предметным smoke-тестом login, чтения каталога и billing/webhook outbox после
  изменений соответствующего контура; после изменения почтовых credentials или
  backend — доставкой тестового сообщения на контролируемый адрес.

### Evidence отдельного timeout Celery Beat

Release 2026-08-29 подтвердил новый shutdown-контракт:

- PR `#261`, свежий полный CI run `33257204165` и exact production commit
  `1c0030532f7fa4bd5357d48b39a5e938261931b5`;
- Deploy run `33257724511` остановил Beat по `SIGTERM` примерно за 10 секунд
  (`15:51:52.687Z`–`15:52:02.922Z`, UTC) и только затем закрыл ingress;
- все десять production services получили `healthy`, topology признана exact,
  внешний `https://dodugir.com/api/v1/ready/` вернул HTTP 200;
- после завершения release repository variable `PROD_DEPLOY_ENABLED` возвращена
  в `false`.

### Автоматическое продление TLS

После первого успешного запуска Nginx установите executable deploy-hook Certbot,
который сначала валидирует конфигурацию внутри production-контейнера, а затем
делает graceful reload:

```bash
sudo ln -sfn /usr/local/sbin/saas-poster-reload-nginx \
  /etc/letsencrypt/renewal-hooks/deploy/saas-poster-nginx-reload
sudo test -x /etc/letsencrypt/renewal-hooks/deploy/saas-poster-nginx-reload
sudo certbot renew --dry-run --run-deploy-hooks
```

Dry-run должен завершиться успешно при работающем production Nginx. Дополнительно
проверьте с внешнего узла, что `https://dodugir.com` отдаёт ожидаемый актуальный
сертификат. После изменения checkout path обновите установленный host contract
отдельным reviewed bootstrap. Hook указывает только на root-owned копию из
`/usr/local/sbin`, перед чтением Compose проверяет владельца/права checkout и не
принимает checkout path из окружения, так как Certbot запускает его с повышенными
привилегиями.

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
