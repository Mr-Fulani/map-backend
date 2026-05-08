#!/usr/bin/env bash
# Резервное копирование PostgreSQL → gzip → Yandex Cloud S3
# Запускать ежедневно через cron: 0 3 * * * /app/scripts/backup.sh
set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="map_backup_${TIMESTAMP}.sql.gz"
RETENTION_DAYS=30

: "${DB_NAME:=map_db}"
: "${DB_USER:=map_user}"
: "${DB_HOST:=db}"
: "${DB_PORT:=5432}"
: "${S3_BUCKET:?S3_BUCKET must be set}"
: "${S3_PREFIX:=backups/postgres}"

echo "[$(date)] Начало резервного копирования: ${BACKUP_FILE}"

# Дамп + сжатие
PGPASSWORD="${DB_PASSWORD:-}" pg_dump \
    -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" \
    --no-password \
    | gzip -9 > "/tmp/${BACKUP_FILE}"

BACKUP_SIZE=$(du -sh "/tmp/${BACKUP_FILE}" | cut -f1)
echo "[$(date)] Дамп создан: ${BACKUP_SIZE}"

# Загрузка в S3 через yc CLI
yc storage object upload \
    --bucket "${S3_BUCKET}" \
    --key "${S3_PREFIX}/${BACKUP_FILE}" \
    --file "/tmp/${BACKUP_FILE}" \
    --content-type "application/gzip"

echo "[$(date)] Загружено в s3://${S3_BUCKET}/${S3_PREFIX}/${BACKUP_FILE}"

# Удаление локального файла
rm -f "/tmp/${BACKUP_FILE}"

# Удаление старых бэкапов из S3 (старше RETENTION_DAYS дней)
CUTOFF=$(date -d "-${RETENTION_DAYS} days" +%Y%m%d 2>/dev/null \
         || date -v "-${RETENTION_DAYS}d" +%Y%m%d)   # macOS fallback

yc storage object list --bucket "${S3_BUCKET}" --prefix "${S3_PREFIX}/" \
    | awk -v cutoff="map_backup_${CUTOFF}" '$1 < cutoff {print $1}' \
    | while read -r key; do
        yc storage object delete --bucket "${S3_BUCKET}" --key "$key"
        echo "[$(date)] Удалён старый бэкап: ${key}"
    done

echo "[$(date)] Резервное копирование завершено"
