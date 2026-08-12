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
- [ ] `flake8`, инкрементальный `mypy` baseline, ShellCheck и
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

- [ ] GitHub environment `production` защищён required reviewers; deploy
      разрешён только для успешного `push` CI в `main`.
- [ ] `PROD_HOST_FINGERPRINT` независимо сверен с SSH host key; парольный SSH и
      agent forwarding не требуются.
- [ ] На хосте `/opt/saas_poster` нет tracked и untracked Git drift и достаточно
      свободного диска (`PROD_MIN_FREE_DISK_MB` плюс запас на параллельную сборку).
- [ ] `.env`, `.backup.env`, `.deploy.env` — обычные non-symlink файлы deploy user,
      имеют mode `600`/`400` и получены из secret manager; `.deploy.env`
      содержит только allowlisted `KEY=value`, секреты не записаны в Git/CI logs.
- [ ] DNS уже указывает на production, а сертификат для домена существует по
      путям, смонтированным в `docker-compose.prod.yml`.
- [ ] `/run/lock/saas-poster` принадлежит deploy user, имеет mode `0700` и
      восстанавливается после reboot через `systemd-tmpfiles`/конфигурацию хоста.
- [ ] Certbot deploy-hook указывает на `scripts/reload_production_nginx.sh`;
      `renew --dry-run --run-deploy-hooks` успешно выполнил `nginx -t`, reload и
      внешнюю проверку обслуживаемого сертификата.
- [ ] `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS`,
      `SITE_URL`, `FRONTEND_URL`, `BILLING_RETURN_URL_ALLOWED_ORIGINS` и
      `PROD_SMOKE_URL` согласованы с текущим HTTPS origin.
- [ ] Platform SMTP настроен только как `smtp.resend.com:587` через
      `http://egress_proxy:3128`; domain-scoped Sending key актуален, а
      `DEFAULT_FROM_EMAIL` является plain email на `notify.dodugir.com`.
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
- [ ] Публичный HTTPS `PROD_SMOKE_URL` (`/api/v1/ready/`) возвращает успех.
- [ ] Проверены предметные сценарии: login/refresh, dashboard, checkout без
      реального списания, webhook/outbox backlog, Celery named ping и Beat
      heartbeat, S3 read/write в разрешённом контуре; после изменения SMTP —
      реальная доставка на контролируемый адрес.
- [ ] В течение согласованного observation window нет роста `5xx`, auth `401`
      loops, duplicate checkout intents, failed webhook deliveries, queue lag,
      worker restarts, DB saturation или backup alerts.

## 6. Ошибка и recovery

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

## 7. Периодические проверки вне release

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
