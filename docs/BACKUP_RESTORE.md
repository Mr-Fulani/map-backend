# Backup и восстановление PostgreSQL

## Гарантии и границы

Production backup создаётся `pg_dump` в custom format, проверяется через
`pg_restore --list`, шифруется `age` и только после этого загружается в private
S3 bucket. В bucket никогда не попадает открытый dump. Для каждого архива рядом
записывается Ed25519-подписанный manifest с SHA-256 зашифрованного файла, версией
PostgreSQL, encoding/collation, commit SHA и контрольными количествами строк
критичных таблиц. `latest.json` обновляется последним, поэтому он не указывает на
частично загруженный backup.

Ключ archive содержит случайный nonce и не переиспользуется. Backup требует
включённый bucket versioning, сохраняет подписанный `VersionId`, а freshness и
restore обращаются именно к этой версии, а не к потенциально заменённому latest
варианту объекта.

Restore:

- запрещает совпадение target с каноническим именем production-БД из
  `RESTORE_PRODUCTION_DATABASE_NAME`;
- требует повторно указать точное имя target в `RESTORE_CONFIRM_DATABASE`;
- принимает только новую пустую БД;
- проверяет checksum **до** расшифровки;
- проверяет Ed25519-подпись доверенным public key **до** расшифровки;
- проверяет версии `pg_restore`, source и target;
- восстанавливает одной транзакцией и останавливается при первой ошибке;
- сверяет Django migration history и контрольные количества строк;
- никогда не переключает production автоматически.

Это восстанавливает PostgreSQL, но не S3 media. Media bucket и ключи шифрования
приложения защищаются отдельно, как описано ниже.

Архив создаётся с `--no-owner --no-acl`: роли, memberships и grants не входят в
DR dump и должны воспроизводиться отдельной проверенной IaC/DBA-процедурой.

## Целевые RPO/RTO

- штатный RPO базы: не более 24 часов;
- freshness alert: после 26 часов без успешного backup;
- обязательный backup непосредственно перед каждой production-миграцией;
- целевой RTO подтверждается только регулярным restore drill, а не фактом
  наличия объектов в bucket.

Если бизнесу нужен меньший RPO, добавьте managed PITR/WAL-архивацию. Ежедневный
logical dump не заменяет point-in-time recovery.

## 1. Bucket и права

Используйте отдельный private bucket, отличный от media bucket. Включите:

1. [versioning](https://yandex.cloud/en/docs/storage/operations/buckets/versioning);
2. server-side encryption провайдера как дополнительный слой;
3. запрет публичного доступа;
4. audit log операций с bucket;
5. [Object Lock/immutability](https://yandex.cloud/en/docs/storage/operations/buckets/configure-object-lock),
   если это поддерживает выбранный тариф;
6. [lifecycle policy](https://yandex.cloud/en/docs/storage/operations/buckets/lifecycles)
   из `ops/s3/backup-lifecycle.json`.

Backup-код сам объекты не удаляет. Service account должен иметь только операции,
необходимые для multipart upload, `HeadObject`, `GetObject`/`GetObjectVersion` и
чтения manifests; не давайте ему `DeleteObject`, изменение bucket policy или
lifecycle. Для реального disaster recovery предпочтительны отдельные read-only
credentials, выдаваемые только на время восстановления.

Restore-контур намеренно отделён от backup-контура: отдельный
`docker-compose.restore.yml` не читает `.env` или `.backup.env`, а принимает
только `RESTORE_*` переменные. Поэтому production S3 writer и Ed25519 private key
не могут попасть в restore-контейнер через штатную конфигурацию.

Имена архивов классифицируются автоматически:

| Класс | Когда создаётся | Пример retention |
|---|---|---:|
| `daily` | последующие успешные backup периода | 35 дней |
| `weekly` | первый успешный backup ISO-недели UTC | 100 дней |
| `monthly` | первый успешный backup месяца UTC | 400 дней |

Если меняется `BACKUP_S3_PREFIX`, синхронно измените prefix-ы lifecycle policy.
Файл использует AWS CLI JSON format. После независимого review примените его
администраторскими credentials, которые не выдаются backup-сервису:

```bash
aws s3api put-bucket-lifecycle-configuration \
  --bucket '<backup-bucket>' \
  --endpoint-url='https://storage.yandexcloud.net' \
  --lifecycle-configuration file://ops/s3/backup-lifecycle.json
```

В versioned bucket срок `NoncurrentVersionExpiration` не должен быть короче
retention текущей версии того же класса. Иначе S3 writer без `DeleteObject` сможет
перезаписать известный key, а lifecycle преждевременно удалит исходный подписанный
`VersionId`. Готовая policy сохраняет noncurrent daily/weekly/monthly версии не
менее 35/100/400 дней; Object Lock остаётся рекомендуемым дополнительным барьером.

## 2. Ключ age

Создайте ключ на доверенной offline/admin машине, а не на production:

```bash
umask 077
age-keygen -o saas-poster-backup-identity.txt
age-keygen -y saas-poster-backup-identity.txt
```

Вторая команда выводит публичный recipient `age1...`. Только его запишите в
`BACKUP_AGE_RECIPIENTS`. Identity file:

- не копируется на production;
- не хранится в Git, `.env`, password manager note без вложения/шифрования;
- имеет минимум две зашифрованные escrow-копии у разных ответственных;
- проверяется во время каждого restore drill;
- ротируется через период `BACKUP_AGE_RECIPIENTS=<new>,<old>`: один архив можно
  расшифровать любым из identities. Старый identity нельзя уничтожать до истечения
  retention всех архивов, созданных только для старого recipient.

Отдельно создайте Ed25519 signing key. Он защищает от подмены archive/manifest
злоумышленником, получившим только S3 write credentials:

```bash
umask 077
install -d -m 700 /secure/saas-poster
python - <<'PY' > /secure/saas-poster/backup-signing.env
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

private = Ed25519PrivateKey.generate()
private_raw = private.private_bytes(
    serialization.Encoding.Raw,
    serialization.PrivateFormat.Raw,
    serialization.NoEncryption(),
)
public_raw = private.public_key().public_bytes(
    serialization.Encoding.Raw,
    serialization.PublicFormat.Raw,
)
print('BACKUP_SIGNING_PRIVATE_KEY=' + base64.b64encode(private_raw).decode())
print('BACKUP_SIGNING_PUBLIC_KEY=' + base64.b64encode(public_raw).decode())
PY
chmod 600 /secure/saas-poster/backup-signing.env
```

Private signing key передаётся production через secret manager. Public key
храните ещё и вне production/S3 и используйте именно эту доверенную копию при
restore. При ротации сохраняйте старые public keys до истечения retention
подписанных ими архивов. Для крупной инфраструктуры предпочтительнее KMS/HSM
signing, но локальная Ed25519-подпись уже отделяет S3 writer от права подмены.

## 3. Production-настройка

На сервере:

```bash
cd /opt/saas_poster
cp .backup.env.example .backup.env
chmod 600 .env .backup.env .deploy.env
```

Заполните `.backup.env`: endpoint/bucket/prefix, отдельные S3 credentials,
публичные `BACKUP_AGE_RECIPIENTS`, signing keys и `BACKUP_DATABASE_URL`.
Backup-контейнер не получает application `.env`, поэтому платёжные, OAuth и
Django-секреты в него не попадают. Private age identity там быть не должно.

Создайте отдельный login без прав записи (команды выполняет DBA, пароль передайте
через secret manager, не через shell history). Role не должна совпадать с
`POSTGRES_USER`, владеть DB/schema/objects, получать другие memberships или
write grants, включая default privileges:

```sql
CREATE ROLE map_backup LOGIN PASSWORD '<generated-secret>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT ON DATABASE map_db TO map_backup;
GRANT pg_read_all_data TO map_backup;
```

`/usr/local/sbin/saas-poster-rotate-backup-db-password` перед каждой ротацией
fail-closed проверяет
эти атрибуты, единственную membership `pg_read_all_data`, object ownership,
effective DML/DDL grants, опасные default ACL и доступные `SECURITY DEFINER`
functions. После изменения схемы проверяйте, что backup role всё ещё читает все
объекты и что новая функция/grant не нарушила этот контракт.

Ротация публикует durable root-only marker
`.backup-db-rotation-uncertain` до `psql \password`. Если marker остался после
ошибки/reboot, не повторяйте ротацию и не удаляйте указанный recovery env.
Порядок безопасной сверки credentials и снятия marker приведён в
[`PRODUCTION_SECURITY.md`](PRODUCTION_SECURITY.md).
Защита restore использует отдельное `RESTORE_PRODUCTION_DATABASE_NAME`: любое
совпадение имени target database с production будет отклонено даже при другом
host/DNS alias или user. Для drill всегда используйте отдельное имя.

Backup image основан на той же major-версии PostgreSQL, что и production DB.
Скрипт дополнительно сравнивает major `pg_dump` с сервером и отказывается работать
при несовпадении. При обновлении PostgreSQL сначала обновите `backup/Dockerfile`,
проверьте restore drill и только потом обновляйте production DB.

Первый ручной запуск:

```bash
production_root="$(pwd -P)"
production_compose=(
  docker compose
  --project-name saas_poster
  --project-directory "$production_root"
  -f "$production_root/docker-compose.prod.yml"
)
"${production_compose[@]}" --profile ops build backup
./scripts/production_backup.sh
./scripts/production_backup_check.sh
```

Успех подтверждён только если команда завершилась с кодом `0`, вывела object key,
размер и SHA-256, а freshness check проверил подпись, companion manifest и HEAD
зашифрованного объекта из bucket.

## 4. Расписание и мониторинг

Готовые units используют root-owned checkout `/opt/saas_poster`. Это соответствует
фактическому owner secret-файлов и не выдаёт отдельному пользователю фиктивную
«ограниченную» привилегию через root-equivalent группу `docker`. Units имеют
systemd hardening и запускают только фиксированные reviewed scripts. Канонический
installer одновременно ставит units, tmpfiles, Certbot hook и ограниченный CI
gateway:

```bash
sudo ./scripts/install_production_host_services.sh /secure/path/mapdeploy.pub
systemctl list-timers 'saas-poster-backup*'
```

`Persistent=true` запускает пропущенную задачу после старта сервера. Freshness
проверяется каждый час. Для независимого dead-man monitor задайте URL в
`.backup.env`; недоступность monitor не превращает уже загруженный архив в
ошибочный, но фиксируется в journal.

Оба production backup wrapper берут тот же root-owned
`/run/lock/saas-poster/deploy.lock`, что и release. Поэтому таймер не читает
checkout и не запускает backup параллельно с build/rollout. Freshness check
никогда не создаёт и не пересоздаёт egress proxy: он требует уже запущенный
healthy proxy и завершается с ошибкой при нарушении этого условия.
Ротация backup DB credential также сериализована этим lock на всём участке
`ALTER ROLE` + atomic env replace; проверочный backup берёт lock отдельным
запуском уже после завершения credential cutover.

Контроль оператора:

```bash
journalctl -u saas-poster-backup.service --since '2 days ago'
journalctl -u saas-poster-backup-check.service --since '2 days ago'
systemctl --failed
```

CI/CD сам выполняет одноразовый зашифрованный backup после Django pre-checks и
до `migrate`. Ошибка dump, шифрования, upload или manifest блокирует миграцию.

### Media bucket

Database backup не содержит S3 media. Для production media bucket отдельно
включите versioning и примените `ops/s3/media-lifecycle.json`: current objects
не имеют expiration, удалённые/заменённые версии сохраняются 365 дней, а
незавершённые multipart uploads очищаются через 7 дней. Bucket остаётся
публичным только для чтения media-объектов; list/config/write не должны быть
публичными. Application key не должен иметь право изменять versioning/lifecycle.
После одноразовой настройки административным credential проверьте статус через
Yandex Cloud API/console и отзовите этот credential из runtime.

Ежедневный DB backup и versioning media закрывают разные сценарии. Ежемесячный
DR drill обязан дополнительно выбрать несколько `ProductImage` ключей из
восстановленной БД и подтвердить, что соответствующие версии читаются из media
bucket; иначе восстановленная БД может ссылаться на отсутствующие изображения.

## 5. Ежемесячный restore drill

Drill выполняйте на отдельном recovery host либо в новой пустой БД с достаточным
запасом диска и CPU. Не используйте имя/URL production. Не запускайте в часы
пикового трафика на общем DB-сервере.

1. Зафиксируйте object key из успешного freshness check или конкретного manifest.
2. Создайте из `template0` новую пустую БД, например `map_restore_202608`, и
   отдельную роль-владельца без superuser. На target не должно быть других client
   sessions, extensions или пользовательских объектов.

Пример для DBA (секрет передайте через защищённый канал, не shell history):

```sql
CREATE ROLE map_restore_202608 LOGIN PASSWORD '<generated-secret>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE map_restore_202608 OWNER map_restore_202608 TEMPLATE template0;
REVOKE CONNECT ON DATABASE map_restore_202608 FROM PUBLIC;
GRANT CONNECT ON DATABASE map_restore_202608 TO map_restore_202608;
```

3. Создайте принадлежащий recovery operator временный каталог с правами `0700`,
   затем доставьте туда age identity по защищённому каналу. После drill удалите
   локальную временную копию по принятой процедуре:

```bash
sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 0700 /secure/drill
```

4. Скопируйте restore-шаблон за пределы checkout. В
   `RESTORE_PRODUCTION_DATABASE_NAME` укажите только каноническое имя production
   database: пароль и сетевой доступ к production restore-процессу не нужны.
5. Создайте защищённый файл через editor (не вводите URL с паролем через `export`
   в командной строке):

```bash
install -m 600 .restore.env.example /secure/drill/restore.env
${EDITOR:?set EDITOR} /secure/drill/restore.env
```

Public signing key берите из независимой доверенной копии. S3 credentials должны
разрешать только чтение конкретного backup prefix (`GetObject`,
`GetObjectVersion`, `HeadObject`) и не должны совпадать с production writer:

```dotenv
RESTORE_PRODUCTION_DATABASE_NAME='map_db'
RESTORE_DATABASE_URL='postgresql://restore_user:secret@restore-postgres.internal:5432/map_restore_202608'
RESTORE_OBJECT_KEY='postgres/monthly/2026/08/20260801T000000Z_abcdef123456_a1b2c3d4e5f6.dump.age'
RESTORE_CONFIRM_DATABASE='map_restore_202608'
RESTORE_AGE_IDENTITY_HOST_FILE='/secure/drill/backup-age-identity.txt'
RESTORE_SIGNING_PUBLIC_KEY='<trusted-base64-public-key>'
RESTORE_S3_BUCKET='map-backups-prod'
RESTORE_S3_PREFIX='postgres'
RESTORE_S3_ENDPOINT='https://storage.yandexcloud.net'
RESTORE_S3_REGION='ru-central1'
RESTORE_S3_ACCESS_KEY='<temporary-read-only-access-key>'
RESTORE_S3_SECRET_KEY='<temporary-read-only-secret-key>'
```

Hostname в `RESTORE_DATABASE_URL` должен указывать на заранее созданный recovery
PostgreSQL, быть разрешимым из временной Compose-сети и не совпадать с production
DB endpoint. В restore Compose нет сервиса с именем `db`.

6. На recovery host создайте единый каталог блокировок, принадлежащий restore
   operator и имеющий mode `0700`. Если host переживает reboot, создавайте его
   через `/etc/tmpfiles.d/saas-poster.conf` по тому же образцу, что и production
   deploy-каталог:

```bash
restore_user="$(id -un)"
restore_group="$(id -gn)"
sudo install -d -o "$restore_user" -g "$restore_group" -m 0700 /run/lock/saas-poster
```

7. Запустите отдельный restore-контур. Скрипт проверит права `400/600` и запретит
   backup writer/private-key переменные. Он использует выделенный фиксированный
   Compose project и единый host-wide lock
   `/run/lock/saas-poster/restore.lock`, общий для всех `RESTORE_ENV_FILE`. Перед новым
   запуском удаляются ресурсы, оставшиеся после `SIGKILL`/потери хоста. Затем
   поднимается собственный `egress_proxy`, собирается одноразовый restore image,
   а при любом штатном завершении удаляются контейнеры, сеть и volume с временным
   открытым dump. Другие Compose-проекты на хосте не затрагиваются:

```bash
RESTORE_ENV_FILE=/secure/drill/restore.env ./scripts/production_restore.sh
```

Recovery-сеть временно имеет прямой маршрут к target PostgreSQL; HTTP(S) трафик
S3 идёт через поднятый Squid благодаря proxy variables. Ограничьте исходящий DB
маршрут host firewall/security group до конкретного target. Если процесс был
убит через `SIGKILL` или хост потерял питание, перед следующим запуском удалите
только его изолированные ресурсы:

```bash
restore_checkout="$(pwd -P)"
docker compose --project-name saas-poster-restore \
  --project-directory "$restore_checkout" \
  --env-file /secure/drill/restore.env \
  -f "$restore_checkout/docker-compose.restore.yml" down --volumes --remove-orphans
```

После drill отзовите временные S3 credentials и удалите локальные secret-файлы
согласно политике команды.

Если automatic cleanup завершился ошибкой, скрипт возвращает ненулевой код даже
после успешного restore и печатает `CRITICAL`: до ручного `down --volumes` нужно
считать plaintext dump оставшимся на диске.

Restore-команда сама проверит checksum, формат archive, пустоту target, версии,
наличие migration history и row counts. Затем в отдельном application validation
environment подставьте только восстановленный `DATABASE_URL`, оставьте workers и
внешние side effects выключенными, выполните `manage.py check --deploy`,
`manage.py migrate --plan` и smoke-тесты чтения. Не используйте для этой проверки
работающий production Compose project.

Дополнительно выборочно проверьте tenants, товары, листинги, счета, последние
операции, временной диапазон данных и чтение зашифрованных credentials. Результат
drill запишите: дата, object key, длительность download/restore/check, объём,
фактический RPO/RTO, версии PostgreSQL, найденные ошибки и ответственный.

## 6. Реальное восстановление и cutover

При инциденте сначала сохраните неисправное состояние для расследования. Restore
всегда выполняется в новую БД. После успешных технических и бизнес-проверок
cutover делает оператор отдельным change request:

1. остановить запись и Celery Beat/workers;
2. определить допустимую точку восстановления и потерю данных;
3. восстановить в новую БД по процедуре выше;
4. проверить приложение и критичные данные;
5. изменить secret `DATABASE_URL` на новый target;
6. запустить приложение, выполнить readiness/smoke и наблюдать метрики;
7. старую БД оставить read-only на согласованный срок.

Скрипты этого репозитория намеренно не выполняют `DROP DATABASE`, `--clean`,
автоматический rollback схемы или автоматический production cutover.

## 7. Media и ключи приложения

DB backup содержит ссылки на media, но не сами файлы. Для media bucket включите
versioning, lifecycle, удалённую репликацию/backup в другой account или region и
периодическую проверку чтения случайной выборки объектов. Service account
приложения не должен иметь право менять bucket policy или backup retention.

`FIELD_ENCRYPTION_KEYS`, `DJANGO_SECRET_KEY`, OAuth/payment credentials и другие
production secrets не кладутся в DB backup. Их нужно хранить и версионировать в
secret manager с отдельным emergency-access процессом. Без escrow исторических
Fernet-ключей восстановленная БД может быть физически цела, но credentials внутри
неё останутся нерасшифровываемыми.

## 8. Регулярный checklist

- ежедневно: backup job успешен, freshness моложе 26 часов;
- еженедельно: нет failed units, bucket не стал публичным, upload объём правдоподобен;
- ежемесячно: restore drill случайно выбранного monthly/weekly архива;
- ежеквартально: проверка IAM, lifecycle/versioning/Object Lock и escrow ключей;
- перед PostgreSQL upgrade: restore старого archive новой версией клиента;
- после изменения схемы критичных данных: актуализировать `CRITICAL_TABLES` и
  restore-проверки.
