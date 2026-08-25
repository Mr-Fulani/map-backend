# Production release checklist

Этот checklist — обязательный gate для каждого production release. Он дополняет,
но не заменяет [`DEPLOYMENT.md`](DEPLOYMENT.md),
[`PRODUCTION_SECURITY.md`](PRODUCTION_SECURITY.md) и
[`BACKUP_RESTORE.md`](BACKUP_RESTORE.md). Любой незакрытый обязательный пункт —
причина отложить release, а не принять риск молча.

## 1. Готовность изменения

- [ ] Release привязан к полному 40-символьному commit SHA из `main`; SHA записан
      в change ticket/журнал релиза.
- [ ] Все изменения прошли review; для auth, billing, webhook, secrets, egress,
      backup и миграций выполнен отдельный security/operability review.
- [ ] Миграции следуют expand/contract, совместимы со старым кодом на время
      rollout и не требуют автоматического reverse migration.
- [ ] Data migrations ограничены по времени и объёму, идемпотентны и имеют
      проверенный forward-recovery путь.
- [ ] Изменения платежей сохраняют idempotency: frontend вращает ключ только при
      явном `checkout_terminal` + `rotate_idempotency_key=true`; неизвестные,
      сетевые и retryable ошибки сохраняют исходный ключ.
- [ ] Пока proration/credit не реализованы, новый subscription checkout для
      активного оплаченного периода отклоняется до Invoice/provider call; для
      trial/billing-only recovery новый срок добавляется после уже обещанного.
- [ ] Для tenant допускается только один незавершённый subscription intent даже
      при разных plan/period и параллельных вкладках.
- [ ] Для новых внешних URL определены DNS/IP admission, redirect policy,
      deadline, response-size limit и egress-proxy доступ.
- [ ] Документация, `.env.example`, OpenAPI и lock-файлы обновлены вместе с кодом.

## 2. Обязательный CI gate

Запуск `CI` именно для release SHA должен завершиться `success`. Не допускается
повторное использование результата другого SHA или ручной пропуск шага.

- [ ] Python 3.12.13 lock-файлы воспроизводимы; hash-locked установка и
      `pip check` успешны.
- [ ] `pip-audit` успешен для production, development, CI и backup lock-файлов.
- [ ] `flake8`, application-wide `mypy` baseline, ShellCheck и
      runtime/deploy/health contracts успешны.
- [ ] PostgreSQL migrations применились на чистом CI database;
      `makemigrations --check --dry-run` не нашёл drift.
- [ ] Полный backend `pytest` и coverage gate успешны.
- [ ] OpenAPI сформирован с `--validate --fail-on-warn` без предупреждений.
- [ ] Frontend установлен через `npm ci --strict-allow-scripts`; `npm audit`,
      production audit, `typecheck`, `lint`, `test:unit` и `build` успешны.
- [ ] Все production/restore images собраны или получены по закреплённым
      digest-ам; Trivy не нашёл fixed HIGH/CRITICAL уязвимостей.
- [ ] Backend, backup и frontend CycloneDX SBOM сохранены как CI artifacts.
- [ ] CI proxy smoke test успешен.

Локальные проверки полезны для быстрого feedback, но не заменяют CI:

```bash
make runtime-check
make frontend-test
cd frontend && npm run typecheck && npm run lint && npm run build
```

Эти команды не обращаются к Docker daemon, кроме явно отсутствующих здесь
Compose-команд. Не запускайте `dev.sh`, `make up`, `make test` или другие Docker
targets, если daemon занят чужим проектом.

## 3. Production preflight

- [ ] GitHub environment `production` создан; deploy разрешён только для
      успешного `push` CI в `main`. Если тариф GitHub поддерживает required
      reviewers для private repository, они включены; иначе merge protection и
      точный успешный CI SHA являются обязательным gate.
- [ ] `mapdeploy` принимает только public-key auth, не состоит в `docker`, не
      читает checkout/secrets и принимает только forced commands `deploy`, `backup-check`,
      `topology-check`; его sudoers прошёл `visudo -c`.
- [ ] `PROD_HOST_FINGERPRINT` независимо сверен с SSH host key; парольный SSH и
      agent forwarding не требуются.
- [ ] На хосте `/opt/saas_poster` нет tracked и untracked Git drift и достаточно
      свободного диска (`PROD_MIN_FREE_DISK_MB` плюс запас на параллельную сборку).
- [ ] Host имеет не менее 3584 MiB RAM; normal Compose memory budget не выше
      2816 MiB, backup не выше 384 MiB, `capacity-check` подтверждает минимум
      1024 MiB доступной памяти для последовательной сборки и отсутствие
      OOM/restart событий.
- [ ] `.env`, `.backup.env`, `.deploy.env` — обычные root-owned non-symlink файлы,
      имеют mode `600`/`400` и получены из secret manager; `.deploy.env`
      содержит только allowlisted `KEY=value`, секреты не записаны в Git/CI logs.
- [ ] DNS уже указывает на production, а сертификат для домена существует по
      путям, смонтированным в `docker-compose.prod.yml`.
- [ ] `/run/lock/saas-poster` принадлежит `root:root`, имеет mode `0700` и
      восстанавливается после reboot через `systemd-tmpfiles`/конфигурацию хоста.
- [ ] Certbot deploy-hook указывает на root-owned
      `/usr/local/sbin/saas-poster-reload-nginx`;
      `renew --dry-run --run-deploy-hooks` успешно выполнил `nginx -t`, reload и
      внешнюю проверку обслуживаемого сертификата.
- [ ] `saas-poster-backup.timer` и `saas-poster-backup-check.timer` enabled и
      active; последние service runs завершились success.
- [ ] Если перед релизом выполнялся host bootstrap, timers были отключены до
      exact target deploy, а `/run/lock/saas-poster/host-contract-pending`
      удалён только после успешных topology и external smoke checks.
- [ ] `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`,
      `SITE_URL`, `FRONTEND_URL`, `BILLING_RETURN_URL_ALLOWED_ORIGINS` и
      `PROD_SMOKE_URL` согласованы с текущим HTTPS origin.
- [ ] Platform SMTP настроен только как `smtp.resend.com:587` через
      `http://egress_proxy:3128`; domain-scoped Sending key актуален, а
      `DEFAULT_FROM_EMAIL` является plain email на `notify.dodugir.com`.
- [ ] Regression доставки подтверждает стабильный `Resend-Idempotency-Key`,
      channel-level dedupe и сохранение Telegram `outcome_uncertain` без replay.
- [ ] Platform credential не используется для tenant sender identities;
      tenant mail требует отдельного verified domain и scoped/BYOK credential.
- [ ] `PUBLIC_HTTP_PROXY_URL` равен строго `http://egress_proxy:3128`;
      Django/workers/Beat/frontend находятся только во внутренней сети, Nginx —
      единственный service в `ingress_public`, а Squid `dst` ACL отклоняет
      private/loopback/link-local/special-use итоговые IP до общего allow.
- [ ] Фиксированный public preflight к корню Yandex Object Storage доступен
      через Squid и не зависит от ingress текущего release.
- [ ] PostgreSQL, cache Redis и durable Celery broker используют отдельные
      случайные credentials; cache и broker не указывают на один endpoint.
      URL-decoded Redis passwords совпадают с raw server passwords, а cache и
      broker raw passwords различаются.
- [ ] При первом переключении broker legacy queues/ETA tasks drained и только
      после этого установлен `PROD_BROKER_MIGRATION_CONFIRMED=true`.
- [ ] Если `BILLING_ENABLED=true`, в YooKassa зарегистрирован точный HTTPS endpoint
      `https://dodugir.com/api/v1/billing/webhook/yookassa/` для
      `payment.succeeded`, `payment.canceled`, `refund.succeeded`; подтверждена
      доставка событий без rejected/retry backlog.
- [ ] Нет длительных imports/exports/billing jobs; maintenance window объявлено.
      `PROD_DRAIN_TIMEOUT_SECONDS` больше максимального Celery hard time limit.

## 4. Backup и восстановимость

- [ ] Последний production backup моложе 26 часов; signature, manifest,
      encrypted object checksum и pinned S3 `VersionId` проверены freshness job.
- [ ] Backup bucket private, versioned, с lifecycle из
      `ops/s3/backup-lifecycle.json`; writer не имеет `DeleteObject`/policy rights.
- [ ] Media bucket versioned, lifecycle соответствует
      `ops/s3/media-lifecycle.json`: current objects бессрочны, noncurrent версии
      хранятся минимум 365 дней; публичны только object reads, не list/config/write.
- [ ] Offline age identity и trusted Ed25519 public key доступны дежурному по DR,
      но отсутствуют на production application/backup runtime.
- [ ] Restore drill за последний месяц успешно восстановил архив в отдельную
      пустую БД и подтвердил migrations + контрольные counts; дата и RTO записаны.
- [ ] Понимается граница backup: database dump не восстанавливает media bucket,
      application encryption keys, DNS, TLS certificates или host configuration.

`deploy.sh` дополнительно создаёт зашифрованный backup после drain и до первой
миграции. Ошибка этого backup обязана остановить release.

## 5. Deploy и наблюдение

- [ ] Deploy workflow получил тот же release SHA; устаревший workflow не
      разворачивается, если `main` уже указывает на другой commit.
- [ ] Для incident/manual deploy предыдущий SHA сохранён до checkout target и
      явно передан как `PREVIOUS_SHA`; запуск без него завершился бы до Docker
      mutation.
- [ ] Preflight, infrastructure readiness, image build, Django pre-deploy checks,
      Redis `PING`, SMTP CONNECT/STARTTLS/login и public HTTPS GET к независимому
      Yandex Object Storage (с проверкой YooKassa credentials только при
      `BILLING_ENABLED=true`) из
      нового image завершились до остановки ingress; SMTP check не отправлял
      письмо, public HTTP check не создавал и не изменял платёж.
- [ ] Старые ingress/Beat/web/workers завершили graceful drain без SIGKILL и
      незавершённых внешних side effects.
- [ ] Pre-migration backup подтверждён до начала `migrate`.
- [ ] Все services (`db`, оба Redis, proxy, Django, оба workers, Beat, frontend,
      Nginx) стали healthy/running.
- [ ] Nginx — единственный service в `ingress_public`, а `docker compose ps`
      показывает фактические host bindings для TCP 80 и 443.
- [ ] `scripts/verify_production_topology.sh` подтвердил точное членство сетей:
      Redis не подключён к legacy/default network, только Nginx имеет ingress,
      только egress proxy имеет внешний egress.
- [ ] Публичный HTTPS `PROD_SMOKE_URL` (`/api/v1/ready/`) возвращает успех.
- [ ] Проверены предметные сценарии: login/refresh, dashboard, checkout без
      реального списания, webhook/outbox backlog, Celery named ping и Beat
      heartbeat, S3 read/write в разрешённом контуре; после изменения SMTP —
      реальная доставка на контролируемый адрес.
- [ ] В течение согласованного observation window нет роста `5xx`, auth `401`
      loops, duplicate checkout intents, failed webhook deliveries, queue lag,
      worker restarts, DB saturation или backup alerts.

## 6. Gate roadmap фидов Avito

- [ ] Release содержит не больше одного пакета из
      [`AVITO_FEED_ROADMAP.md`](AVITO_FEED_ROADMAP.md), а его точный состав
      соответствует
      [`AVITO_FEED_CHANGESET_MANIFEST.md`](AVITO_FEED_CHANGESET_MANIFEST.md).
- [ ] P0 не содержит runtime-код, настройки приложения, миграции или тесты
      будущих пакетов.
- [ ] Для P1 и последующих пакетов указаны точные узкие и полные test commands
      с результатами; следующий пакет не используется для исправления текущего.
- [ ] Production feed contract остаётся
      `legacy/legacy/disabled/false/legacy_public` для run, ingress, artifact,
      profile migration и storage, либо для отдельно утверждённого P5
      observation — `legacy/dual_write/disabled/false/legacy_public` вместе с
      `AVITO_STATUS_LIFECYCLE_MODE=dual_write` и готовым атомарным rollback обоих
      режимов в `legacy`.
- [ ] Cleanup, `0039`, private serving, GC, object deletion, новые
      migrations/modes и worker activation отсутствуют, если соответствующий
      roadmap package не был отдельно активирован.
- [ ] Cleanup/backfill и auto-applied `0039` не объединены в один release.

## 7. Ошибка и recovery

- [ ] При ошибке **до** начала миграций проверен автоматический возврат на
      сохранённые image IDs предыдущего SHA и повторный public smoke check.
- [ ] После `MIGRATIONS_STARTED=true` старый release вручную не запускается.
      Writers остаются остановленными, состояние `django_migrations` и backup
      фиксируются, исправление проходит обычный CI и разворачивается forward.
- [ ] Reverse migration или restore/cutover выполняются только по отдельно
      reviewed плану; restore никогда не направляется прямо в canonical
      production database.
- [ ] Инцидент, фактические времена drain/backup/migrate/readiness и решение
      rollback/forward recovery записаны в журнал релиза.

## 8. Периодические проверки вне release

- [ ] Еженедельно проверяются dependency updates, pinned GitHub Actions/base
      image digests и Trivy release checksum. Direct Trivy asset не обновляется
      Dependabot — version, URL и официальный SHA-256 меняются вместе.
- [ ] Ежемесячно выполняется restore drill и проверяется RTO.
- [ ] После каждого реального TLS renewal проверены Nginx reload и сертификат,
      фактически обслуживаемый внешнему клиенту.
- [ ] Ежеквартально ротируются/проверяются credentials, SSH host fingerprint,
      webhook secrets, SMTP login, Fernet key-ring и DR escrow.
- [ ] После смены домена одновременно обновляются Nginx config, TLS mounts,
      Django/browser origins, billing return allowlist, DNS и smoke URL.

## Sign-off

| Поле | Значение |
|---|---|
| Release SHA | |
| CI run URL | |
| Ответственный reviewer | |
| Maintenance window | |
| Последний backup / VersionId | |
| Последний restore drill / RTO | |
| Deploy result | |
| Observation window завершено | |
