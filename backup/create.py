#!/usr/bin/env python3
import logging
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from backup.common import (
    BACKUP_FORMAT_VERSION,
    BackupConfigError,
    S3Settings,
    collect_snapshot_database_metadata,
    collect_snapshot_row_counts,
    exclusive_lock,
    exported_snapshot,
    json_bytes,
    load_json_body,
    notify_monitor_safely,
    parse_utc,
    positive_int_env,
    postgres_environment,
    postgres_server_major,
    required_env,
    run_checked,
    sha256_file,
    sign_manifest,
    tool_major_version,
    utc_now,
    validate_manifest,
    validate_signing_key_pair,
    verify_manifest_signature,
)


logger = logging.getLogger('backup')
MIN_TEMP_FREE_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class BackupSettings:
    backup_database_url: str
    age_recipients: tuple[str, ...]
    s3: S3Settings
    state_dir: Path
    git_sha: str
    success_monitor_url: str | None
    failure_monitor_url: str | None
    signing_private_key: str
    signing_public_key: str
    command_timeout_seconds: int

    @classmethod
    def from_env(cls):
        git_sha = os.environ.get('DEPLOY_GIT_SHA', 'unknown').strip() or 'unknown'
        age_recipients = tuple(
            recipient.strip()
            for recipient in required_env('BACKUP_AGE_RECIPIENTS').split(',')
            if recipient.strip()
        )
        if not age_recipients:
            raise BackupConfigError(
                'BACKUP_AGE_RECIPIENTS must contain at least one recipient'
            )
        settings = cls(
            backup_database_url=required_env('BACKUP_DATABASE_URL'),
            age_recipients=age_recipients,
            s3=S3Settings.from_env(),
            state_dir=Path(os.environ.get('BACKUP_STATE_DIR', '/state')),
            git_sha=git_sha[:64],
            success_monitor_url=os.environ.get('BACKUP_MONITOR_SUCCESS_URL') or None,
            failure_monitor_url=os.environ.get('BACKUP_MONITOR_FAILURE_URL') or None,
            signing_private_key=required_env('BACKUP_SIGNING_PRIVATE_KEY'),
            signing_public_key=required_env('BACKUP_SIGNING_PUBLIC_KEY'),
            command_timeout_seconds=positive_int_env(
                'BACKUP_COMMAND_TIMEOUT_SECONDS',
                3600,
            ),
        )
        validate_signing_key_pair(
            settings.signing_private_key,
            settings.signing_public_key,
        )
        return settings


def _get_object_if_exists(client, *, bucket: str, key: str):
    from botocore.exceptions import ClientError

    try:
        return client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get('Error', {}).get('Code', ''))
        if code in {'404', 'NoSuchKey', 'NotFound'}:
            return None
        raise


def _coverage_marker_valid(
    client,
    *,
    bucket: str,
    key: str,
    public_key_value: str,
    created_at,
    period: str,
) -> bool:
    response = _get_object_if_exists(client, bucket=bucket, key=key)
    if response is None:
        return False
    try:
        manifest = validate_manifest(
            load_json_body(response['Body'], label=f'coverage marker {key}')
        )
        verify_manifest_signature(manifest, public_key_value)
        manifest_created_at = parse_utc(manifest['created_at'])
    except (ValueError, KeyError) as exc:
        logger.warning('Ignoring invalid coverage marker %s: %s', key, type(exc).__name__)
        return False

    if period == 'monthly':
        return (
            manifest['retention_class'] == 'monthly'
            and manifest_created_at.year == created_at.year
            and manifest_created_at.month == created_at.month
        )
    return (
        manifest['retention_class'] in {'monthly', 'weekly'}
        and manifest_created_at.isocalendar()[:2] == created_at.isocalendar()[:2]
    )


def retention_plan(
    client,
    s3: S3Settings,
    created_at,
    public_key_value: str,
) -> tuple[str, list[str]]:
    iso_year, iso_week, _ = created_at.isocalendar()
    monthly_marker = s3.key(f'coverage/monthly/{created_at:%Y-%m}.json')
    weekly_marker = s3.key(f'coverage/weekly/{iso_year}-W{iso_week:02d}.json')
    monthly_exists = _coverage_marker_valid(
        client,
        bucket=s3.bucket,
        key=monthly_marker,
        public_key_value=public_key_value,
        created_at=created_at,
        period='monthly',
    )
    weekly_exists = _coverage_marker_valid(
        client,
        bucket=s3.bucket,
        key=weekly_marker,
        public_key_value=public_key_value,
        created_at=created_at,
        period='weekly',
    )

    if not monthly_exists:
        markers = [monthly_marker]
        if not weekly_exists:
            markers.append(weekly_marker)
        return 'monthly', markers
    if not weekly_exists:
        return 'weekly', [weekly_marker]
    return 'daily', []


def assert_temp_capacity(connection, workdir: Path) -> int:
    database_size = int(
        connection.execute('SELECT pg_database_size(current_database())').fetchone()[0]
    )
    required_free = max(MIN_TEMP_FREE_BYTES, database_size * 2)
    actual_free = shutil.disk_usage(workdir).free
    if actual_free < required_free:
        raise RuntimeError(
            'Insufficient temporary disk space for dump plus encrypted archive: '
            f'free={actual_free} required={required_free}'
        )
    return database_size


def create_backup(settings: BackupSettings) -> dict:
    if not settings.age_recipients:
        raise BackupConfigError(
            'BACKUP_AGE_RECIPIENTS must contain at least one recipient'
        )
    validate_signing_key_pair(
        settings.signing_private_key,
        settings.signing_public_key,
    )
    created_at = utc_now()
    timestamp = created_at.strftime('%Y%m%dT%H%M%SZ')
    short_sha = settings.git_sha[:12] if settings.git_sha != 'unknown' else 'unknown'
    object_nonce = secrets.token_hex(6)

    with exclusive_lock(settings.state_dir / 'backup.lock'):
        client = settings.s3.client()
        retention_class, coverage_keys = retention_plan(
            client,
            settings.s3,
            created_at,
            settings.signing_public_key,
        )
        object_name = (
            f'{retention_class}/{created_at:%Y/%m}/'
            f'{timestamp}_{short_sha}_{object_nonce}.dump.age'
        )
        object_key = settings.s3.key(object_name)
        manifest_key = object_key.removesuffix('.dump.age') + '.manifest.json'
        latest_key = settings.s3.key('latest.json')

        with tempfile.TemporaryDirectory(prefix='postgres-backup-') as temp_dir:
            workdir = Path(temp_dir)
            dump_file = workdir / 'database.dump'
            encrypted_file = workdir / 'database.dump.age'

            with postgres_environment(
                settings.backup_database_url,
                workdir,
                name='BACKUP_DATABASE_URL',
            ) as (target, pg_env):
                server_major = postgres_server_major(target, pg_env)
                client_major = tool_major_version('pg_dump')
                if client_major != server_major:
                    raise RuntimeError(
                        f'pg_dump major {client_major} does not match server major {server_major}'
                    )

                with exported_snapshot(settings.backup_database_url) as (
                    connection,
                    snapshot_id,
                ):
                    database_size = assert_temp_capacity(connection, workdir)
                    database_metadata = collect_snapshot_database_metadata(connection)
                    row_counts = collect_snapshot_row_counts(connection)
                    run_checked([
                        'pg_dump',
                        *target.cli_args(),
                        f'--snapshot={snapshot_id}',
                        '--format=custom',
                        '--compress=9',
                        '--no-owner',
                        '--no-acl',
                        '--file', str(dump_file),
                    ], env=pg_env, timeout=settings.command_timeout_seconds)

            run_checked(
                ['pg_restore', '--list', str(dump_file)],
                capture_output=True,
                timeout=settings.command_timeout_seconds,
            )
            age_args = ['age']
            for recipient in settings.age_recipients:
                age_args.extend(['--recipient', recipient])
            age_args.extend([
                '--output', str(encrypted_file),
                str(dump_file),
            ])
            run_checked(age_args, timeout=settings.command_timeout_seconds)

            checksum = sha256_file(encrypted_file)
            encrypted_size = encrypted_file.stat().st_size
            client.upload_file(
                str(encrypted_file),
                settings.s3.bucket,
                object_key,
                ExtraArgs={
                    'ContentType': 'application/octet-stream',
                    'Metadata': {'sha256': checksum},
                },
            )
            archive_head = client.head_object(
                Bucket=settings.s3.bucket,
                Key=object_key,
            )
            if archive_head.get('ContentLength') != encrypted_size:
                raise RuntimeError('Uploaded archive size differs from the local file')
            if archive_head.get('Metadata', {}).get('sha256') != checksum:
                raise RuntimeError('Uploaded archive checksum metadata is missing or invalid')
            archive_version_id = archive_head.get('VersionId')
            if not archive_version_id or archive_version_id == 'null':
                raise RuntimeError('Backup bucket versioning is required')

            manifest = sign_manifest({
                'format_version': BACKUP_FORMAT_VERSION,
                'created_at': created_at.isoformat().replace('+00:00', 'Z'),
                'object_key': object_key,
                'manifest_key': manifest_key,
                'sha256': checksum,
                'encrypted_size_bytes': encrypted_size,
                'archive_version_id': archive_version_id,
                'postgres_major': server_major,
                'database_size_bytes': database_size,
                'database_metadata': database_metadata,
                'git_sha': settings.git_sha,
                'retention_class': retention_class,
                'row_counts': row_counts,
            }, settings.signing_private_key)
            verify_manifest_signature(manifest, settings.signing_public_key)

            client.put_object(
                Bucket=settings.s3.bucket,
                Key=manifest_key,
                Body=json_bytes(manifest),
                ContentType='application/json',
            )
            for coverage_key in coverage_keys:
                client.put_object(
                    Bucket=settings.s3.bucket,
                    Key=coverage_key,
                    Body=json_bytes(manifest),
                    ContentType='application/json',
                )
            client.put_object(
                Bucket=settings.s3.bucket,
                Key=latest_key,
                Body=json_bytes(manifest),
                ContentType='application/json',
            )

    notify_monitor_safely(settings.success_monitor_url, {
        'status': 'ok',
        'created_at': manifest['created_at'],
        'object_key': object_key,
    }, logger)
    return manifest


def main() -> int:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    settings = None
    try:
        settings = BackupSettings.from_env()
        manifest = create_backup(settings)
        logger.info(
            'Encrypted backup uploaded: key=%s bytes=%s sha256=%s',
            manifest['object_key'],
            manifest['encrypted_size_bytes'],
            manifest['sha256'],
        )
        return 0
    except Exception as exc:
        logger.exception('Backup failed')
        failure_url = (
            settings.failure_monitor_url
            if settings is not None
            else os.environ.get('BACKUP_MONITOR_FAILURE_URL')
        )
        try:
            notify_monitor_safely(failure_url, {
                'status': 'error',
                'error_type': type(exc).__name__,
            }, logger)
        except Exception:
            logger.exception('Failure monitor notification also failed')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
