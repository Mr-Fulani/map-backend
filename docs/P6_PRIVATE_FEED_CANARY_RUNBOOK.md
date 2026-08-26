# P6 private feed: bounded production canary

Этот runbook применяется только к одному заранее проверенному Avito-аккаунту.
Он не разрешает P7, GC, удаление объектов, `0039` или широкое worker wiring.
`MARKETPLACE_FEED_ARTIFACT_MODE=active` допустим только после отдельного
решения владельца продукта и только с exact-one account allowlist из раздела
7.

## Неизменяемые условия

- production checkout и deployed image имеют один точный SHA, Git clean;
- `MARKETPLACE_FEED_RUN_MODE=legacy`;
- P5 ingress и Avito lifecycle одновременно `dual_write`;
- массовая миграция профилей выключена до и после отдельного P4-перехода;
- private bucket закрыт от public access, versioning включён, default
  encryption использует один утверждённый KMS key;
- private static key не совпадает с media key и не имеет
  `s3:DeleteObject`/`s3:DeleteObjectVersion`;
- `GetBucketAcl` либо возвращает точный ожидаемый Yandex folder ID, либо
  Yandex опускает одновременно оба owner-поля; частичный или другой owner
  всегда блокирует операцию;
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

### 5.1 Audited reconciliation одного `put_pending`

Recovery допустим только после доказанного завершения исходной process
boundary и не менее 15 минут settlement. Для Docker допустимой boundary
является init-процесс уже уничтоженного контейнера: reference обязан быть
привязан к incident/redeploy evidence, а время завершения берётся как
консервативная верхняя граница из Docker metadata. PID без уникального
container/process reference использовать нельзя.

Yandex документирует [strong consistency для PUT/DELETE][yandex-consistency]
и отдельный [`ListObjectVersions`][yandex-list-versions] для versioned bucket.
Перед apply оператор отдельно
фиксирует revision реального exact-key empty-list canary. Команда имеет только
read-only list client, сверяет точные tenant/account/endpoint/run/attempt UUID
и revision, а audit сохраняет HMAC-дайджесты references вместо их открытых
значений:

```bash
python manage.py reconcile_private_feed_artifact_put \
  --tenant-id TENANT_ID \
  --account-id ACCOUNT_ID \
  --endpoint-id ENDPOINT_UUID \
  --run-id RUN_UUID \
  --attempt-id ATTEMPT_UUID \
  --expected-attempt-revision ATTEMPT_REVISION \
  --origin-process-id TERMINATED_BOUNDARY_PID \
  --origin-process-terminated-at ISO_8601_WITH_TIMEZONE \
  --termination-evidence-reference INCIDENT_REFERENCE \
  --operator-reference OPERATOR_REFERENCE \
  --origin-process-reference UNIQUE_ORIGIN_REFERENCE \
  --identity-digest-key-revision KEY_REVISION \
  --canary-policy-revision EXACT_LIST_CANARY_REVISION \
  --apply \
  --canary \
  --confirm-account-id ACCOUNT_ID \
  --confirm-origin-process-terminated
```

Результат `no_object` разрешает только новую immutable attempt с номером N+1;
старый key не переиспользуется. Найденная версия сохраняется как
`version_known`; delete marker, несколько версий или malformed listing
переводят attempt в `manual_review`. Ни одна ветка ничего не удаляет.

### 5.2 Safe resume того же generation

Resume требует точные attempt/run UUID и revision. Поддерживаются только две
явные ветки: audited `no_object` после сверки неизвестного PUT либо последняя
`version_known` с VersionId из ответа PUT. Команда заново строит XML, сравнивает
SHA-256/размер/количество со frozen run, проверяет отсутствие более новой
attempt и только затем claim-ит тот же generation:

```bash
python manage.py canary_private_feed_artifact \
  --account-id ACCOUNT_ID \
  --phase resume \
  --expected-run-id RUN_UUID \
  --expected-run-revision RUN_REVISION \
  --expected-attempt-id RECONCILED_ATTEMPT_UUID \
  --expected-attempt-revision RECONCILED_ATTEMPT_REVISION \
  --apply \
  --canary \
  --confirm-account-id ACCOUNT_ID
```

Для `no_object` дополнительно проверяется неизменённый reconciliation audit;
только эта ветка создаёт immutable attempt N+1 и может выполнить новый PUT.
Для `version_known` новый PUT запрещён: сервис делает HEAD и GET только точной
версии, после чего либо атомарно прикрепляет её, либо останавливается.

Yandex Object Storage может вернуть имена `X-Amz-Meta-*` с другим регистром и
не включить `ChecksumSHA256` в PUT/HEAD/GET response. Имена metadata
сравниваются без учёта HTTP-регистра, но значения остаются точными; коллизии
регистра запрещены. Отсутствующий provider checksum принимается только после
совпадения VersionId, типа, размера, immutable metadata и полного SHA-256
GET-readback exact version. Присутствующий, но отличный checksum всегда
останавливает проверку.

Любое несовпадение fence останавливает resume до storage-вызова. Если новый
PUT ветки `no_object` снова становится неизвестным, он остаётся новой
`put_pending` attempt и проходит отдельную сверку; автоматического retry нет.

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

## 7. Постоянный account-scoped cutover после успешного canary

Постоянное включение не меняет fleet run mode и не затрагивает тестовые
аккаунты. Для единственного разрешённого account используются одновременно:

```text
MARKETPLACE_FEED_RUN_MODE=legacy
MARKETPLACE_FEED_INGRESS_MODE=dual_write
AVITO_STATUS_LIFECYCLE_MODE=dual_write
MARKETPLACE_FEED_ARTIFACT_MODE=active
MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS=ACCOUNT_ID
MARKETPLACE_FEED_STORAGE_MODE=stable_bridge
MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED=false
```

Production settings отклоняют `active` без канонического списка ровно из
одного положительного ID. Private serving в `active` также проверяет этот ID,
поэтому случайная private endpoint запись другого аккаунта остаётся тёмной.

После deploy оператор повторно проверяет legacy endpoint и запускает:

```bash
python manage.py activate_marketplace_feed_cutover \
  --account-id ACCOUNT_ID \
  --confirm-account-id ACCOUNT_ID \
  --apply
```

Команда делает bucket preflight, проверяет владельца endpoint, verified
профиль и отсутствие uncertain run, затем создаёт или переиспользует ровно
один durable intent. Worker строит новую immutable generation, выполняет
one-shot private PUT, exact-version HEAD/GET, атомарно переключает тот же
stable endpoint и только затем запускает Avito Autoload. Потерянный ответ PUT
не повторяется; endpoint до reconciliation продолжает legacy serving.

Для экстренного отката сначала выполняется exact artifact rollback из раздела
6 при всё ещё включённом `active` admission. Только после подтверждённого
legacy 307 удаляется `ACCOUNT_ID` из allowlist, artifact mode возвращается в
`disabled`, и сервисы перезапускаются. Старый private artifact, VersionId,
upload ledger и evidence сохраняются. Повторное включение требует новой
source intent generation; запрещено вручную переиспользовать старый PUT.

[yandex-consistency]: https://yandex.cloud/en/docs/storage/qa#what-data-consistency-model-does-yandex-object-storage-use
[yandex-list-versions]: https://yandex.cloud/en/docs/storage/s3/api-ref/bucket/listObjectVersions
