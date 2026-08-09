#!/usr/bin/env python3
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from backup.common import (
    S3Settings,
    collect_database_metadata,
    collect_row_counts,
    load_json_body,
    parse_database_url,
    postgres_environment,
    postgres_server_major,
    positive_int_env,
    psql_scalar,
    required_env,
    run_checked,
    sha256_file,
    tool_major_version,
    validate_manifest,
    verify_manifest_signature,
)


logger = logging.getLogger('restore')
MIN_TEMP_FREE_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True)
class RestoreSettings:
    restore_database_url: str
    production_database_name: str
    object_key: str
    age_identity_file: Path
    confirmed_database: str
    signing_public_key: str
    command_timeout_seconds: int
    s3: S3Settings

    @classmethod
    def from_env(cls):
        return cls(
            restore_database_url=required_env('RESTORE_DATABASE_URL'),
            production_database_name=required_env(
                'RESTORE_PRODUCTION_DATABASE_NAME'
            ),
            object_key=required_env('RESTORE_OBJECT_KEY'),
            age_identity_file=Path(required_env('RESTORE_AGE_IDENTITY_FILE')),
            confirmed_database=required_env('RESTORE_CONFIRM_DATABASE'),
            signing_public_key=required_env('RESTORE_SIGNING_PUBLIC_KEY'),
            command_timeout_seconds=positive_int_env(
                'RESTORE_COMMAND_TIMEOUT_SECONDS',
                7200,
            ),
            s3=S3Settings.from_env(prefix='RESTORE'),
        ).validate()

    def validate(self):
        if (
            not self.age_identity_file.is_file()
            or self.age_identity_file.is_symlink()
        ):
            raise ValueError('RESTORE_AGE_IDENTITY_FILE does not exist')
        identity_mode = stat.S_IMODE(self.age_identity_file.stat().st_mode)
        if identity_mode & 0o077:
            raise ValueError(
                'RESTORE_AGE_IDENTITY_FILE must not be accessible by group or others'
            )

        restore_target = parse_database_url(
            'RESTORE_DATABASE_URL', self.restore_database_url,
        )
        if restore_target.database.casefold() in {'postgres', 'template0', 'template1'}:
            raise ValueError('Restore into a PostgreSQL system database is forbidden')
        if (
            restore_target.database.casefold()
            == self.production_database_name.casefold()
        ):
            raise ValueError('Restore into the production database is forbidden')
        if self.confirmed_database != restore_target.database:
            raise ValueError(
                'RESTORE_CONFIRM_DATABASE must exactly match the restore database name'
            )
        return self


def _load_manifest(client, settings: RestoreSettings) -> dict:
    manifest_key = settings.object_key.removesuffix('.dump.age') + '.manifest.json'
    response = client.get_object(Bucket=settings.s3.bucket, Key=manifest_key)
    manifest = load_json_body(response['Body'], label='backup manifest')
    validate_manifest(manifest, expected_object_key=settings.object_key)
    verify_manifest_signature(manifest, settings.signing_public_key)
    return manifest


def _assert_empty_database(target, pg_env):
    is_superuser = psql_scalar(
        target,
        pg_env,
        "SELECT rolsuper::int FROM pg_roles WHERE rolname = current_user;",
    )
    if is_superuser != '0':
        raise ValueError('RESTORE_DATABASE_URL must use a non-superuser role')

    other_sessions = int(psql_scalar(
        target,
        pg_env,
        """
        SELECT count(*)
        FROM pg_stat_activity
        WHERE datname = current_database()
          AND pid <> pg_backend_pid()
          AND backend_type = 'client backend';
        """,
    ))
    if other_sessions != 0:
        raise ValueError('Restore target has other active client sessions')

    object_count = int(psql_scalar(
        target,
        pg_env,
        """
        WITH user_namespaces AS (
            SELECT oid
            FROM pg_catalog.pg_namespace
            WHERE nspname NOT IN ('pg_catalog', 'information_schema')
              AND nspname NOT LIKE 'pg_toast%'
        )
        SELECT
            (SELECT count(*) FROM pg_catalog.pg_class
             WHERE relnamespace IN (SELECT oid FROM user_namespaces))
          + (SELECT count(*) FROM pg_catalog.pg_proc
             WHERE pronamespace IN (SELECT oid FROM user_namespaces))
          + (SELECT count(*) FROM pg_catalog.pg_type
             WHERE typnamespace IN (SELECT oid FROM user_namespaces)
               AND typtype IN ('b', 'c', 'd', 'e', 'm', 'r'))
          + (SELECT count(*) FROM pg_catalog.pg_namespace
             WHERE oid IN (SELECT oid FROM user_namespaces)
               AND nspname <> 'public')
          + (SELECT count(*) FROM pg_catalog.pg_extension
             WHERE extname <> 'plpgsql')
          + (SELECT count(*) FROM pg_catalog.pg_event_trigger);
        """,
    ))
    if object_count != 0:
        raise ValueError(
            'RESTORE_DATABASE_URL must point to a new database without user objects'
        )


def restore_backup(settings: RestoreSettings) -> dict:
    settings.validate()
    client = settings.s3.client()
    manifest = _load_manifest(client, settings)
    archive_head = client.head_object(
        Bucket=settings.s3.bucket,
        Key=settings.object_key,
        VersionId=manifest['archive_version_id'],
    )
    if archive_head.get('ContentLength') != manifest['encrypted_size_bytes']:
        raise ValueError('Encrypted archive size differs from signed manifest')
    if archive_head.get('Metadata', {}).get('sha256') != manifest['sha256']:
        raise ValueError('Encrypted archive metadata differs from signed manifest')

    with tempfile.TemporaryDirectory(prefix='postgres-restore-') as temp_dir:
        workdir = Path(temp_dir)
        required_free = max(
            MIN_TEMP_FREE_BYTES,
            manifest['encrypted_size_bytes'] + manifest['database_size_bytes'],
        )
        actual_free = shutil.disk_usage(workdir).free
        if actual_free < required_free:
            raise RuntimeError(
                'Insufficient temporary disk space for encrypted archive plus dump: '
                f'free={actual_free} required={required_free}'
            )
        encrypted_file = workdir / 'database.dump.age'
        dump_file = workdir / 'database.dump'
        client.download_file(
            settings.s3.bucket,
            settings.object_key,
            str(encrypted_file),
            ExtraArgs={'VersionId': manifest['archive_version_id']},
        )

        actual_checksum = sha256_file(encrypted_file)
        if actual_checksum != manifest.get('sha256'):
            raise ValueError('Encrypted backup checksum mismatch')

        run_checked([
            'age',
            '--decrypt',
            '--identity', str(settings.age_identity_file),
            '--output', str(dump_file),
            str(encrypted_file),
        ], timeout=settings.command_timeout_seconds)
        run_checked(
            ['pg_restore', '--list', str(dump_file)],
            capture_output=True,
            timeout=settings.command_timeout_seconds,
        )

        with postgres_environment(
            settings.restore_database_url,
            workdir,
            name='RESTORE_DATABASE_URL',
        ) as (target, pg_env):
            _assert_empty_database(target, pg_env)
            source_major = int(manifest['postgres_major'])
            target_major = postgres_server_major(target, pg_env)
            client_major = tool_major_version('pg_restore')
            if target_major < source_major or client_major != target_major:
                raise RuntimeError(
                    'Restore target must not be older than the source and pg_restore '
                    'must match the target major version'
                )
            target_metadata = collect_database_metadata(target, pg_env)
            if target_metadata != manifest['database_metadata']:
                raise RuntimeError(
                    'Restore target encoding/collation differs from the backup source'
                )

            run_checked([
                'pg_restore',
                *target.cli_args(),
                '--exit-on-error',
                '--single-transaction',
                '--no-owner',
                '--no-acl',
                str(dump_file),
            ], env=pg_env, timeout=settings.command_timeout_seconds)
            run_checked(
                [
                    'psql',
                    '--no-psqlrc',
                    '--set=ON_ERROR_STOP=1',
                    *target.cli_args(),
                    '--command',
                    'ANALYZE;',
                ],
                env=pg_env,
                timeout=settings.command_timeout_seconds,
            )
            migration_count = int(psql_scalar(
                target,
                pg_env,
                'SELECT count(*) FROM "public"."django_migrations";',
            ))
            if migration_count <= 0:
                raise RuntimeError('Restored database has no Django migration history')

            restored_counts = collect_row_counts(target, pg_env)
            if restored_counts != manifest.get('row_counts'):
                raise RuntimeError('Critical table row counts differ from backup manifest')

    return {
        'object_key': settings.object_key,
        'postgres_major': manifest['postgres_major'],
        'migration_count': migration_count,
        'row_counts': restored_counts,
    }


def main() -> int:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        result = restore_backup(RestoreSettings.from_env())
        logger.info(
            'Restore drill completed: key=%s migrations=%s rows=%s',
            result['object_key'],
            result['migration_count'],
            result['row_counts'],
        )
        return 0
    except Exception:
        logger.exception('Restore failed; production was not switched automatically')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
