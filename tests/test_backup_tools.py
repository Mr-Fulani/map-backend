import base64
import contextlib
import datetime as dt
import hashlib
import io
import json
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backup import create
from backup.check_freshness import check_freshness
from backup.common import (
    BACKUP_FORMAT_VERSION,
    BackupConfigError,
    CRITICAL_TABLES,
    DatabaseTarget,
    S3Settings,
    parse_database_url,
    postgres_environment,
    sign_manifest,
)
from backup.restore import RestoreSettings, _assert_empty_database, restore_backup


SIGNING_PRIVATE_KEY_BYTES = b'\x01' * 32
SIGNING_PRIVATE_KEY = base64.b64encode(SIGNING_PRIVATE_KEY_BYTES).decode()
SIGNING_PUBLIC_KEY = base64.b64encode(
    Ed25519PrivateKey.from_private_bytes(SIGNING_PRIVATE_KEY_BYTES)
    .public_key()
    .public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
).decode()


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.metadata = {}
        self.operations = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.operations.append(('upload_file', key))
        self.objects[(bucket, key)] = Path(filename).read_bytes()
        self.metadata[(bucket, key)] = (ExtraArgs or {}).get('Metadata', {})

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.operations.append(('put_object', Key))
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {'Error': {'Code': 'NoSuchKey', 'Message': 'Not Found'}},
                'GetObject',
            )
        return {'Body': io.BytesIO(self.objects[(Bucket, Key)])}

    def download_file(self, bucket, key, filename, ExtraArgs=None):
        assert (ExtraArgs or {}).get('VersionId') == 'fake-version-1'
        Path(filename).write_bytes(self.objects[(bucket, key)])

    def head_object(self, *, Bucket, Key, VersionId=None):
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {'Error': {'Code': '404', 'Message': 'Not Found'}},
                'HeadObject',
            )
        payload = self.objects[(Bucket, Key)]
        return {
            'ContentLength': len(payload),
            'Metadata': self.metadata.get((Bucket, Key), {}),
            'VersionId': 'fake-version-1',
        }


class FakeS3Settings:
    bucket = 'backup-bucket'

    def __init__(self, client, prefix='postgres'):
        self._client = client
        self.prefix = prefix

    def client(self):
        return self._client

    def key(self, suffix):
        return f'{self.prefix}/{suffix}' if self.prefix else suffix


def test_parse_database_url_requires_complete_postgres_credentials():
    target = parse_database_url(
        'DATABASE_URL',
        'postgresql://map%40user:p%3Ass@db.internal:5544/map%2Ddb?sslmode=require',
    )
    assert target == DatabaseTarget(
        host='db.internal',
        port=5544,
        username='map@user',
        password='p:ss',
        database='map-db',
        sslmode='require',
        query='sslmode=require',
    )
    cli_args = target.cli_args()
    assert 'p:ss' not in ' '.join(cli_args)
    assert 'sslmode=require' in ' '.join(cli_args)

    with pytest.raises(BackupConfigError, match='password'):
        parse_database_url('DATABASE_URL', 'postgresql://map@db.internal/map')
    with pytest.raises(BackupConfigError, match='postgres'):
        parse_database_url('DATABASE_URL', 'mysql://map:secret@db.internal/map')
    with pytest.raises(BackupConfigError, match='forbidden query options'):
        parse_database_url(
            'DATABASE_URL',
            'postgresql://map:secret@db.internal/map?password=exposed',
        )


def test_postgres_environment_uses_private_pgpass_and_removes_it(tmp_path):
    database_url = 'postgresql://map:p%3Aa%5Css@db:5432/map'
    with postgres_environment(database_url, tmp_path) as (_, process_env):
        pgpass = Path(process_env['PGPASSFILE'])
        assert stat.S_IMODE(pgpass.stat().st_mode) == 0o600
        assert pgpass.read_text() == 'db:5432:map:map:p\\:a\\\\ss\n'
    assert not pgpass.exists()


def test_retention_plan_promotes_first_success_in_period(monkeypatch):
    created_at = dt.datetime(2026, 8, 3, tzinfo=dt.UTC)
    client = FakeS3Client()
    s3 = FakeS3Settings(client)
    monthly = 'postgres/coverage/monthly/2026-08.json'
    weekly = 'postgres/coverage/weekly/2026-W32.json'

    covered = set()
    monkeypatch.setattr(
        create,
        '_coverage_marker_valid',
        lambda client, *, key, **kwargs: key in covered,
    )

    assert create.retention_plan(client, s3, created_at, SIGNING_PUBLIC_KEY) == (
        'monthly',
        [monthly, weekly],
    )
    covered.add(monthly)
    assert create.retention_plan(
        client, s3, created_at, SIGNING_PUBLIC_KEY,
    ) == ('weekly', [weekly])
    covered.add(weekly)
    assert create.retention_plan(
        client, s3, created_at, SIGNING_PUBLIC_KEY,
    ) == ('daily', [])


def test_create_uploads_archive_then_manifest_then_latest(monkeypatch, tmp_path):
    fixed_now = dt.datetime(2026, 8, 1, 0, 0, tzinfo=dt.UTC)
    client = FakeS3Client()
    s3 = FakeS3Settings(client)
    settings = create.BackupSettings(
        backup_database_url='postgresql://map:secret@db:5432/map',
        age_recipients=('age1public', 'age1rotation'),
        s3=s3,
        state_dir=tmp_path,
        git_sha='abcdef1234567890',
        success_monitor_url=None,
        failure_monitor_url=None,
        signing_private_key=SIGNING_PRIVATE_KEY,
        signing_public_key=SIGNING_PUBLIC_KEY,
        command_timeout_seconds=60,
    )
    target = DatabaseTarget('db', 5432, 'map', 'secret', 'map')

    @contextlib.contextmanager
    def fake_postgres_environment(*args, **kwargs):
        yield target, {'PGPASSFILE': '/private/pgpass'}

    @contextlib.contextmanager
    def fake_snapshot(*args, **kwargs):
        yield object(), '00000003-0000001B-1'

    command_timeouts = []

    def fake_run_checked(args, **kwargs):
        command_timeouts.append(kwargs.get('timeout'))
        if args[0] == 'pg_dump':
            Path(args[args.index('--file') + 1]).write_bytes(b'custom-dump')
            assert '--snapshot=00000003-0000001B-1' in args
        elif args[0] == 'age':
            assert args.count('--recipient') == 2
            Path(args[args.index('--output') + 1]).write_bytes(b'encrypted-dump')
        return subprocess.CompletedProcess(args, 0, stdout='archive-list')

    monkeypatch.setattr(create, 'utc_now', lambda: fixed_now)
    monkeypatch.setattr(create, 'postgres_environment', fake_postgres_environment)
    monkeypatch.setattr(create, 'postgres_server_major', lambda *args: 16)
    monkeypatch.setattr(create, 'tool_major_version', lambda *args: 16)
    monkeypatch.setattr(create, 'exported_snapshot', fake_snapshot)
    monkeypatch.setattr(create, 'assert_temp_capacity', lambda *args: 1024)
    monkeypatch.setattr(
        create,
        'collect_snapshot_database_metadata',
        lambda connection: {'encoding': 'UTF8', 'collate': 'C', 'ctype': 'C'},
    )
    monkeypatch.setattr(
        create,
        'collect_snapshot_row_counts',
        lambda connection: {table: 4 for table in CRITICAL_TABLES},
    )
    monkeypatch.setattr(create, 'run_checked', fake_run_checked)

    manifest = create.create_backup(settings)

    assert manifest['retention_class'] == 'monthly'
    assert manifest['object_key'].startswith('postgres/monthly/2026/08/')
    assert client.operations == [
        ('upload_file', manifest['object_key']),
        ('put_object', manifest['manifest_key']),
        ('put_object', 'postgres/coverage/monthly/2026-08.json'),
        ('put_object', 'postgres/coverage/weekly/2026-W31.json'),
        ('put_object', 'postgres/latest.json'),
    ]
    uploaded_manifest = json.loads(
        client.objects[('backup-bucket', manifest['manifest_key'])]
    )
    latest_manifest = json.loads(client.objects[('backup-bucket', 'postgres/latest.json')])
    assert uploaded_manifest == manifest == latest_manifest
    assert command_timeouts == [60, 60, 60]


def test_backup_refuses_insufficient_temporary_space(monkeypatch, tmp_path):
    class Connection:
        def execute(self, query):
            return SimpleNamespace(fetchone=lambda: (2 * 1024 * 1024 * 1024,))

    monkeypatch.setattr(
        create.shutil,
        'disk_usage',
        lambda path: SimpleNamespace(free=3 * 1024 * 1024 * 1024),
    )
    with pytest.raises(RuntimeError, match='Insufficient temporary disk'):
        create.assert_temp_capacity(Connection(), tmp_path)


def _s3_environment(monkeypatch):
    values = {
        'BACKUP_S3_BUCKET': 'backups',
        'BACKUP_S3_ENDPOINT': 'https://s3.example.test',
        'BACKUP_S3_ACCESS_KEY': 'access',
        'BACKUP_S3_SECRET_KEY': 'secret',
        'BACKUP_SIGNING_PUBLIC_KEY': SIGNING_PUBLIC_KEY,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _restore_environment(monkeypatch):
    values = {
        'RESTORE_S3_BUCKET': 'backups',
        'RESTORE_S3_ENDPOINT': 'https://s3.example.test',
        'RESTORE_S3_ACCESS_KEY': 'read-access',
        'RESTORE_S3_SECRET_KEY': 'read-secret',
        'RESTORE_SIGNING_PUBLIC_KEY': SIGNING_PUBLIC_KEY,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_restore_settings_forbid_production_and_require_exact_confirmation(
    monkeypatch,
    tmp_path,
):
    identity = tmp_path / 'age.key'
    identity.write_text('AGE-SECRET-KEY-test')
    identity.chmod(0o600)
    _restore_environment(monkeypatch)
    monkeypatch.setenv('RESTORE_PRODUCTION_DATABASE_NAME', 'map')
    monkeypatch.setenv(
        'RESTORE_DATABASE_URL',
        'postgresql://restore:secret@database-alias:5432/map',
    )
    monkeypatch.setenv('RESTORE_OBJECT_KEY', 'postgres/monthly/example.dump.age')
    monkeypatch.setenv('RESTORE_CONFIRM_DATABASE', 'map')
    monkeypatch.setenv('RESTORE_AGE_IDENTITY_FILE', str(identity))

    with pytest.raises(ValueError, match='production database'):
        RestoreSettings.from_env()

    monkeypatch.setenv(
        'RESTORE_DATABASE_URL',
        'postgresql://restore:secret@db:5432/map_restore',
    )
    with pytest.raises(ValueError, match='exactly match'):
        RestoreSettings.from_env()


def test_direct_restore_call_cannot_bypass_production_guard(tmp_path):
    identity = tmp_path / 'age.key'
    identity.write_text('AGE-SECRET-KEY-test')
    identity.chmod(0o600)
    settings = RestoreSettings(
        restore_database_url='postgresql://restore:secret@alias:5432/map',
        production_database_name='map',
        object_key='postgres/monthly/example.dump.age',
        age_identity_file=identity,
        confirmed_database='map',
        signing_public_key=SIGNING_PUBLIC_KEY,
        command_timeout_seconds=60,
        s3=FakeS3Settings(FakeS3Client()),
    )

    with pytest.raises(ValueError, match='production database'):
        restore_backup(settings)


def test_restore_rejects_system_database_and_weak_identity_permissions(tmp_path):
    identity = tmp_path / 'age.key'
    identity.write_text('AGE-SECRET-KEY-test')
    identity.chmod(0o600)
    settings = RestoreSettings(
        restore_database_url='postgresql://restore:secret@db:5432/postgres',
        production_database_name='map',
        object_key='postgres/monthly/example.dump.age',
        age_identity_file=identity,
        confirmed_database='postgres',
        signing_public_key=SIGNING_PUBLIC_KEY,
        command_timeout_seconds=60,
        s3=FakeS3Settings(FakeS3Client()),
    )

    with pytest.raises(ValueError, match='system database'):
        settings.validate()

    identity.chmod(0o644)
    settings = replace(
        settings,
        restore_database_url='postgresql://restore:secret@db:5432/map_restore',
        confirmed_database='map_restore',
    )
    with pytest.raises(ValueError, match='group or others'):
        settings.validate()


def test_restore_rejects_checksum_before_decrypt(monkeypatch, tmp_path):
    client = FakeS3Client()
    object_key = 'postgres/monthly/example.dump.age'
    manifest_key = object_key.removesuffix('.dump.age') + '.manifest.json'
    client.objects[('backup-bucket', object_key)] = b'tampered'
    client.metadata[('backup-bucket', object_key)] = {'sha256': '0' * 64}
    manifest = sign_manifest({
        'format_version': BACKUP_FORMAT_VERSION,
        'created_at': '2026-08-01T00:00:00Z',
        'object_key': object_key,
        'manifest_key': manifest_key,
        'sha256': '0' * 64,
        'encrypted_size_bytes': len(b'tampered'),
        'archive_version_id': 'fake-version-1',
        'postgres_major': 16,
        'database_size_bytes': 1024,
        'database_metadata': {'encoding': 'UTF8', 'collate': 'C', 'ctype': 'C'},
        'retention_class': 'monthly',
        'row_counts': {table: 0 for table in CRITICAL_TABLES},
    }, SIGNING_PRIVATE_KEY)
    client.objects[('backup-bucket', manifest_key)] = json.dumps(manifest).encode()
    identity = tmp_path / 'age.key'
    identity.write_text('AGE-SECRET-KEY-test')
    identity.chmod(0o600)
    settings = RestoreSettings(
        restore_database_url='postgresql://restore:secret@db:5432/map_restore',
        production_database_name='map',
        object_key=object_key,
        age_identity_file=identity,
        confirmed_database='map_restore',
        signing_public_key=SIGNING_PUBLIC_KEY,
        command_timeout_seconds=60,
        s3=FakeS3Settings(client),
    )
    run_calls = []
    monkeypatch.setattr(
        'backup.restore.run_checked',
        lambda *args, **kwargs: run_calls.append(args),
    )

    with pytest.raises(ValueError, match='checksum'):
        restore_backup(settings)
    assert run_calls == []


def test_freshness_rejects_stale_and_future_manifest():
    now = dt.datetime(2026, 8, 9, 12, 0, tzinfo=dt.UTC)
    client = FakeS3Client()
    s3 = FakeS3Settings(client)

    def set_latest(created_at):
        archive = b'encrypted-archive'
        object_key = 'postgres/daily/example.dump.age'
        manifest_key = 'postgres/daily/example.manifest.json'
        checksum = hashlib.sha256(archive).hexdigest()
        manifest = sign_manifest({
            'format_version': BACKUP_FORMAT_VERSION,
            'created_at': created_at,
            'object_key': object_key,
            'manifest_key': manifest_key,
            'sha256': checksum,
            'encrypted_size_bytes': len(archive),
            'archive_version_id': 'fake-version-1',
            'postgres_major': 16,
            'database_size_bytes': 1024,
            'database_metadata': {'encoding': 'UTF8', 'collate': 'C', 'ctype': 'C'},
            'retention_class': 'daily',
            'row_counts': {table: 0 for table in CRITICAL_TABLES},
        }, SIGNING_PRIVATE_KEY)
        payload = json.dumps(manifest).encode()
        client.objects[('backup-bucket', 'postgres/latest.json')] = payload
        client.objects[('backup-bucket', manifest_key)] = payload
        client.objects[('backup-bucket', object_key)] = archive
        client.metadata[('backup-bucket', object_key)] = {'sha256': checksum}

    set_latest('2026-08-08T08:00:00Z')
    with pytest.raises(BackupConfigError, match='stale'):
        check_freshness(
            s3,
            SIGNING_PUBLIC_KEY,
            now=now,
            max_age_seconds=93600,
        )

    set_latest('2026-08-09T11:30:00Z')
    result = check_freshness(
        s3,
        SIGNING_PUBLIC_KEY,
        now=now,
        max_age_seconds=93600,
    )
    assert result['age_seconds'] == 1800

    forged = json.loads(client.objects[('backup-bucket', 'postgres/latest.json')])
    forged['created_at'] = '2026-08-09T11:59:00Z'
    client.objects[('backup-bucket', 'postgres/latest.json')] = json.dumps(forged).encode()
    with pytest.raises(BackupConfigError, match='signature verification'):
        check_freshness(
            s3,
            SIGNING_PUBLIC_KEY,
            now=now,
            max_age_seconds=93600,
        )

    set_latest('2026-08-09T12:06:00Z')
    with pytest.raises(BackupConfigError, match='future'):
        check_freshness(
            s3,
            SIGNING_PUBLIC_KEY,
            now=now,
            max_age_seconds=93600,
        )


def test_restore_target_must_be_non_superuser_empty_and_quiescent(monkeypatch):
    target = DatabaseTarget('db', 5432, 'restore', 'secret', 'map_restore')

    responses = iter(['1'])
    monkeypatch.setattr('backup.restore.psql_scalar', lambda *args: next(responses))
    with pytest.raises(ValueError, match='non-superuser'):
        _assert_empty_database(target, {})

    responses = iter(['0', '1'])
    monkeypatch.setattr('backup.restore.psql_scalar', lambda *args: next(responses))
    with pytest.raises(ValueError, match='active client'):
        _assert_empty_database(target, {})

    responses = iter(['0', '0', '1'])
    monkeypatch.setattr('backup.restore.psql_scalar', lambda *args: next(responses))
    with pytest.raises(ValueError, match='without user objects'):
        _assert_empty_database(target, {})


def test_backup_container_and_deploy_contracts():
    root = Path(__file__).resolve().parents[1]
    dockerignore = (root / '.dockerignore').read_text()
    dockerfile = (root / 'backup/Dockerfile').read_text()
    deploy = (root / 'deploy.sh').read_text()

    assert '.backup.env*' in dockerignore
    assert '.restore.env*' in dockerignore
    assert '.deploy.env*' in dockerignore
    assert '*signing*.env' in dockerignore
    assert dockerfile.startswith('FROM postgres:16-alpine')
    assert 'USER postgres' in dockerfile
    assert 'BACKUP_AGE_IDENTITY_FILE' not in dockerfile

    backup_position = deploy.index('pre-migration database backup')
    migration_position = deploy.index('DEPLOY_PHASE="database migration"')
    assert backup_position < migration_position
    assert '--profile ops run --rm --no-deps' in deploy


def test_restore_settings_require_dedicated_read_only_environment(
    monkeypatch,
    tmp_path,
):
    identity = tmp_path / 'age.key'
    identity.write_text('AGE-SECRET-KEY-test')
    identity.chmod(0o600)
    _s3_environment(monkeypatch)
    monkeypatch.setenv('RESTORE_PRODUCTION_DATABASE_NAME', 'map')
    monkeypatch.setenv(
        'RESTORE_DATABASE_URL',
        'postgresql://restore:secret@db:5432/map_restore',
    )
    monkeypatch.setenv('RESTORE_OBJECT_KEY', 'postgres/daily/example.dump.age')
    monkeypatch.setenv('RESTORE_CONFIRM_DATABASE', 'map_restore')
    monkeypatch.setenv('RESTORE_AGE_IDENTITY_FILE', str(identity))

    with pytest.raises(BackupConfigError, match='RESTORE_SIGNING_PUBLIC_KEY'):
        RestoreSettings.from_env()


def test_s3_settings_do_not_fall_back_to_application_media_credentials(monkeypatch):
    monkeypatch.setenv('YC_S3_ACCESS_KEY', 'media-access')
    monkeypatch.setenv('YC_S3_SECRET_KEY', 'media-secret')
    monkeypatch.delenv('BACKUP_S3_ACCESS_KEY', raising=False)
    monkeypatch.delenv('BACKUP_S3_SECRET_KEY', raising=False)
    monkeypatch.setenv('BACKUP_S3_BUCKET', 'backups')
    monkeypatch.setenv('BACKUP_S3_ENDPOINT', 'https://s3.example.test')

    with pytest.raises(BackupConfigError, match='BACKUP_S3_ACCESS_KEY'):
        S3Settings.from_env()
