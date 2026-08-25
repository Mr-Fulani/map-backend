# P6 private feed: bounded production canary

Этот runbook применяется только к одному заранее проверенному Avito-аккаунту.
Он не разрешает P7, GC, удаление объектов, `0039`, durable worker или
`MARKETPLACE_FEED_ARTIFACT_MODE=active`.

## Неизменяемые условия

- production checkout и deployed image имеют один точный SHA, Git clean;
- `MARKETPLACE_FEED_RUN_MODE=legacy`;
- P5 ingress и Avito lifecycle одновременно `dual_write`;
- массовая миграция профилей выключена до и после отдельного P4-перехода;
- private bucket закрыт от public access, versioning включён, default
  encryption использует один утверждённый KMS key;
- private static key не совпадает с media key и не имеет
  `s3:DeleteObject`/`s3:DeleteObjectVersion`;
- `GetBucketAcl` возвращает точный ожидаемый Yandex folder ID;
- публичный stable URL и capability не меняются при private promotion.

При несовпадении любого условия canary не начинается.

## 1. Выключенный release

Сначала код и миграции `0029`–`0030` выкладываются с:

```text
MARKETPLACE_FEED_ARTIFACT_MODE=disabled
MARKETPLACE_FEED_ARTIFACT_BUCKET=
MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID=
MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY=
MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER=
MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID=
MARKETPLACE_FEED_ARTIFACT_MAX_BYTES=268435456
MARKETPLACE_FEED_REDIRECT_TTL_SECONDS=120
```

После миграций проверяются readiness, все контейнеры, applied migration head,
логи и неизменившаяся legacy-выдача.

## 2. Один stable legacy endpoint

P6 не меняет URL в Avito. Выбранный аккаунт сначала должен пройти уже
выпущенный P4 workflow: prepare, реальный HTTP 307 legacy bridge, migrate и
provider readback. В базе требуется ровно один endpoint со следующими
свойствами:

```text
storage_mode=legacy_bridge
serve_enabled=true
profile_state=verified
legacy_object_key непустой
source_intent_revision = account.feed_intent_revision
```

Другие аккаунты не подготавливаются и не мигрируются.

## 3. Read-only P6 inspect

```bash
python manage.py canary_private_feed_artifact \
  --account-id ACCOUNT_ID \
  --phase inspect
```

Проверяются точные account/endpoint revisions, отсутствие due legacy work,
лимит до 10 000 объявлений и `runtime_ready`. Вывод команды не содержит URL,
capability, credentials, bucket/object key или VersionId.

## 4. Private canary activation

После согласованного рестарта используются только:

```text
MARKETPLACE_FEED_ARTIFACT_MODE=canary
MARKETPLACE_FEED_STORAGE_MODE=private_generation
MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false
MARKETPLACE_FEED_RUN_MODE=legacy
```

Ingress/lifecycle остаются парно `dual_write`. Затем один оператор запускает:

```bash
python manage.py canary_private_feed_artifact \
  --account-id ACCOUNT_ID \
  --phase activate \
  --apply \
  --canary \
  --confirm-account-id ACCOUNT_ID
```

Команда до PUT повторно проверяет folder owner, versioning и KMS. XML строится
в disk-backed tempfile. Пустая публикация получает Avito STOP, непустая —
детерминированный потоковый XML. PUT выполняется ровно одной SDK-попыткой;
после этого точный VersionId читается и сверяется по SHA-256. Только затем
run завершается и тот же endpoint атомарно переключается legacy → private.

После activation проверяются:

- endpoint указывает на exact artifact/revision;
- stable URL отвечает 307 только для GET/HEAD и валидной capability;
- redirect содержит exact `versionId` и TTL 120 секунд;
- перед каждым выданным redirect зафиксирован fetch evidence;
- Avito получает XML, а readiness/контейнеры/логи остаются зелёными;
- ни один второй endpoint не перешёл в `private_generation`.

## 5. Fail-closed до promotion

Если PUT завершился с неизвестным результатом, его нельзя повторять. Upload
ledger остаётся `put_pending`, endpoint продолжает выдавать legacy XML, а
canary считается неуспешным. Оператор использует только bounded reconciliation
из `feed_artifact_put_reconciliation.py`; до точного решения новая попытка для
этого generation запрещена. Любая известная лишняя версия сохраняется:
удаление относится к замороженному P7.

## 6. Точный rollback после promotion

Из результата activation берутся `artifact_id` и `artifact_revision`:

```bash
python manage.py canary_private_feed_artifact \
  --account-id ACCOUNT_ID \
  --phase rollback \
  --apply \
  --canary \
  --confirm-account-id ACCOUNT_ID \
  --expected-artifact-id ARTIFACT_UUID \
  --expected-artifact-revision ARTIFACT_REVISION
```

Rollback выполняется только при полном совпадении account, endpoint, current
artifact и revision. Он меняет только `private_generation` → `legacy_bridge`.
Stable URL, legacy object, artifact, VersionId, upload ledger и fetch evidence
не удаляются и не отвязываются.

После проверки legacy 307 настройки возвращаются к
`MARKETPLACE_FEED_ARTIFACT_MODE=disabled` и
`MARKETPLACE_FEED_STORAGE_MODE=stable_bridge`. Для повторного canary требуется
новое отдельное решение и новая точная generation.
