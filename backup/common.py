import contextlib
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit


BACKUP_FORMAT_VERSION = 1
MAX_MANIFEST_SIZE_BYTES = 1024 * 1024
CRITICAL_TABLES = (
    'tenants_tenant',
    'products_product',
    'marketplaces_listing',
    'billing_invoice',
)


class BackupConfigError(ValueError):
    pass


def required_env(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise BackupConfigError(f'{name} is required')
    return value


def object_metadata_value(response: dict, name: str) -> str | None:
    """Read S3 user metadata without assuming provider key casing.

    AWS returns metadata keys lower-cased, while Yandex Object Storage may
    preserve title casing (for example ``Sha256``). Reject ambiguous duplicate
    spellings instead of silently trusting one of them.
    """
    metadata = response.get('Metadata')
    if not isinstance(metadata, dict):
        return None
    matches = [
        value
        for key, value in metadata.items()
        if isinstance(key, str) and key.casefold() == name.casefold()
    ]
    if len(matches) != 1 or not isinstance(matches[0], str):
        return None
    return matches[0]


def positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise BackupConfigError(f'{name} must be an integer') from exc
    if value <= 0:
        raise BackupConfigError(f'{name} must be positive')
    return value


@dataclass(frozen=True)
class DatabaseTarget:
    host: str
    port: int
    username: str
    password: str
    database: str
    sslmode: str | None = None
    query: str = ''

    @property
    def identity(self) -> tuple[str, int, str]:
        return self.host, self.port, self.database

    def cli_args(self) -> list[str]:
        host = f'[{self.host}]' if ':' in self.host else self.host
        netloc = f'{quote(self.username, safe="")}@{host}:{self.port}'
        connection_url = urlunsplit((
            'postgresql',
            netloc,
            f'/{quote(self.database, safe="")}',
            self.query,
            '',
        ))
        return ['--dbname', connection_url]


def parse_database_url(name: str, value: str) -> DatabaseTarget:
    parsed = urlsplit(value)
    if parsed.scheme not in {'postgres', 'postgresql'}:
        raise BackupConfigError(f'{name} must use postgres:// or postgresql://')
    if not parsed.hostname or not parsed.username or parsed.password is None:
        raise BackupConfigError(f'{name} must contain host, username and password')

    database = unquote(parsed.path.lstrip('/'))
    if not database:
        raise BackupConfigError(f'{name} must contain a database name')
    if parsed.fragment:
        raise BackupConfigError(f'{name} must not contain a URL fragment')

    query = parse_qs(parsed.query, keep_blank_values=True)
    forbidden_options = {
        'database',
        'dbname',
        'host',
        'hostaddr',
        'passfile',
        'password',
        'port',
        'service',
        'servicefile',
        'user',
    }
    conflicting_options = forbidden_options.intersection(query)
    if conflicting_options:
        options = ', '.join(sorted(conflicting_options))
        raise BackupConfigError(f'{name} contains forbidden query options: {options}')
    sslmode = query.get('sslmode', [None])[0]
    return DatabaseTarget(
        host=parsed.hostname,
        port=parsed.port or 5432,
        username=unquote(parsed.username),
        password=unquote(parsed.password),
        database=database,
        sslmode=sslmode,
        query=parsed.query,
    )


def _pgpass_escape(value: str) -> str:
    return value.replace('\\', '\\\\').replace(':', '\\:')


@contextlib.contextmanager
def postgres_environment(
    database_url: str,
    workdir: Path,
    name: str = 'DATABASE_URL',
):
    target = parse_database_url(name, database_url)
    pgpass_file = workdir / '.pgpass'
    pgpass_file.write_text(':'.join([
        _pgpass_escape(target.host),
        str(target.port),
        _pgpass_escape(target.database),
        _pgpass_escape(target.username),
        _pgpass_escape(target.password),
    ]) + '\n')
    pgpass_file.chmod(0o600)

    process_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith('PG')
    }
    process_env['PGPASSFILE'] = str(pgpass_file)
    try:
        yield target, process_env
    finally:
        pgpass_file.unlink(missing_ok=True)


def run_checked(
    args: list[str],
    *,
    env=None,
    capture_output=False,
    timeout=None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        env=env,
        check=True,
        text=True,
        capture_output=capture_output,
        timeout=timeout,
    )


def tool_major_version(tool: str) -> int:
    output = run_checked(
        [tool, '--version'],
        capture_output=True,
        timeout=10,
    ).stdout
    match = re.search(r'(\d+)(?:\.\d+)?', output)
    if not match:
        raise BackupConfigError(f'Cannot parse {tool} version: {output.strip()}')
    return int(match.group(1))


def psql_scalar(target: DatabaseTarget, process_env: dict, sql: str) -> str:
    result = run_checked(
        [
            'psql',
            '--no-psqlrc',
            '--set=ON_ERROR_STOP=1',
            *target.cli_args(),
            '--tuples-only',
            '--no-align',
            '--command', sql,
        ],
        env=process_env,
        capture_output=True,
        timeout=60,
    )
    return result.stdout.strip()


def postgres_server_major(target: DatabaseTarget, process_env: dict) -> int:
    version_num = int(psql_scalar(target, process_env, 'SHOW server_version_num;'))
    return version_num // 10000


def collect_row_counts(target: DatabaseTarget, process_env: dict) -> dict[str, int]:
    counts = {}
    for table in CRITICAL_TABLES:
        if not re.fullmatch(r'[a-z][a-z0-9_]*', table):
            raise BackupConfigError(f'Unsafe table name: {table}')
        counts[table] = int(psql_scalar(
            target,
            process_env,
            f'SELECT count(*) FROM "public"."{table}";',
        ))
    return counts


def collect_snapshot_row_counts(connection) -> dict[str, int]:
    from psycopg import sql

    counts = {}
    for table in CRITICAL_TABLES:
        if not re.fullmatch(r'[a-z][a-z0-9_]*', table):
            raise BackupConfigError(f'Unsafe table name: {table}')
        query = sql.SQL('SELECT count(*) FROM {}.{}').format(
            sql.Identifier('public'),
            sql.Identifier(table),
        )
        counts[table] = int(connection.execute(query).fetchone()[0])
    return counts


def collect_snapshot_database_metadata(connection) -> dict[str, str]:
    row = connection.execute(
        """
        SELECT pg_encoding_to_char(encoding), datcollate, datctype
        FROM pg_catalog.pg_database
        WHERE datname = current_database()
        """
    ).fetchone()
    return {
        'encoding': row[0],
        'collate': row[1],
        'ctype': row[2],
    }


def collect_database_metadata(
    target: DatabaseTarget,
    process_env: dict,
) -> dict[str, str]:
    payload = psql_scalar(
        target,
        process_env,
        """
        SELECT json_build_object(
            'encoding', pg_encoding_to_char(encoding),
            'collate', datcollate,
            'ctype', datctype
        )::text
        FROM pg_catalog.pg_database
        WHERE datname = current_database();
        """,
    )
    value = json.loads(payload)
    return {key: str(value[key]) for key in ('encoding', 'collate', 'ctype')}


@contextlib.contextmanager
def exported_snapshot(database_url: str):
    import psycopg

    connection = psycopg.connect(database_url, autocommit=True)
    try:
        connection.execute(
            'BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'
        )
        snapshot_row = connection.execute('SELECT pg_export_snapshot()').fetchone()
        if snapshot_row is None or not snapshot_row[0]:
            raise RuntimeError('PostgreSQL did not return an exported snapshot identifier.')
        snapshot_id = str(snapshot_row[0])
        yield connection, snapshot_id
    finally:
        try:
            connection.execute('ROLLBACK')
        finally:
            connection.close()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')


def load_json_body(body, *, label: str) -> dict:
    payload = body.read(MAX_MANIFEST_SIZE_BYTES + 1)
    if len(payload) > MAX_MANIFEST_SIZE_BYTES:
        raise BackupConfigError(f'{label} exceeds the maximum allowed size')
    try:
        value = json.loads(payload.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupConfigError(f'{label} is not valid UTF-8 JSON') from exc
    if not isinstance(value, dict):
        raise BackupConfigError(f'{label} must contain a JSON object')
    return value


def validate_manifest(manifest: dict, *, expected_object_key: str | None = None) -> dict:
    if manifest.get('format_version') != BACKUP_FORMAT_VERSION:
        raise BackupConfigError('Unsupported backup format version')

    object_key = manifest.get('object_key')
    if not isinstance(object_key, str) or not object_key.endswith('.dump.age'):
        raise BackupConfigError('Manifest contains an invalid object_key')
    if expected_object_key is not None and object_key != expected_object_key:
        raise BackupConfigError('Manifest object_key does not match requested object')

    expected_manifest_key = object_key.removesuffix('.dump.age') + '.manifest.json'
    if manifest.get('manifest_key') != expected_manifest_key:
        raise BackupConfigError('Manifest contains an invalid manifest_key')
    if not re.fullmatch(r'[0-9a-f]{64}', str(manifest.get('sha256', ''))):
        raise BackupConfigError('Manifest contains an invalid SHA-256 checksum')

    encrypted_size = manifest.get('encrypted_size_bytes')
    if not isinstance(encrypted_size, int) or isinstance(encrypted_size, bool):
        raise BackupConfigError('Manifest contains an invalid encrypted size')
    if encrypted_size <= 0:
        raise BackupConfigError('Manifest contains an invalid encrypted size')
    archive_version_id = manifest.get('archive_version_id')
    if not isinstance(archive_version_id, str) or not archive_version_id:
        raise BackupConfigError('Manifest contains an invalid archive version ID')

    postgres_major = manifest.get('postgres_major')
    if not isinstance(postgres_major, int) or isinstance(postgres_major, bool):
        raise BackupConfigError('Manifest contains an invalid PostgreSQL major version')
    if postgres_major <= 0:
        raise BackupConfigError('Manifest contains an invalid PostgreSQL major version')

    database_size = manifest.get('database_size_bytes')
    if not isinstance(database_size, int) or isinstance(database_size, bool):
        raise BackupConfigError('Manifest contains an invalid database size')
    if database_size <= 0:
        raise BackupConfigError('Manifest contains an invalid database size')

    database_metadata = manifest.get('database_metadata')
    if not isinstance(database_metadata, dict):
        raise BackupConfigError('Manifest contains invalid database metadata')
    for key in ('encoding', 'collate', 'ctype'):
        if not isinstance(database_metadata.get(key), str) or not database_metadata[key]:
            raise BackupConfigError(f'Manifest database metadata is invalid for {key}')

    retention_class = manifest.get('retention_class')
    if retention_class not in {'daily', 'weekly', 'monthly'}:
        raise BackupConfigError('Manifest contains an invalid retention class')
    parse_utc(str(manifest.get('created_at', '')))

    row_counts = manifest.get('row_counts')
    if not isinstance(row_counts, dict):
        raise BackupConfigError('Manifest contains invalid row counts')
    for table in CRITICAL_TABLES:
        value = row_counts.get(table)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise BackupConfigError(f'Manifest row count is invalid for {table}')
    if manifest.get('signature_algorithm') != 'ed25519':
        raise BackupConfigError('Manifest has an unsupported signature algorithm')
    try:
        signature = base64.b64decode(manifest.get('signature', ''), validate=True)
    except (TypeError, ValueError) as exc:
        raise BackupConfigError('Manifest contains an invalid signature') from exc
    if len(signature) != 64:
        raise BackupConfigError('Manifest contains an invalid signature')
    return manifest


def _decode_ed25519_key(value: str, *, name: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise BackupConfigError(f'{name} must be valid base64') from exc
    if len(key) != 32:
        raise BackupConfigError(f'{name} must encode exactly 32 bytes')
    return key


def sign_manifest(manifest: dict, private_key_value: str) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if 'signature' in manifest or 'signature_algorithm' in manifest:
        raise BackupConfigError('Manifest is already signed')
    signed = {**manifest, 'signature_algorithm': 'ed25519'}
    private_key = Ed25519PrivateKey.from_private_bytes(
        _decode_ed25519_key(
            private_key_value,
            name='BACKUP_SIGNING_PRIVATE_KEY',
        )
    )
    signed['signature'] = base64.b64encode(
        private_key.sign(json_bytes(signed))
    ).decode('ascii')
    return signed


def validate_signing_key_pair(
    private_key_value: str,
    public_key_value: str,
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.from_private_bytes(
        _decode_ed25519_key(
            private_key_value,
            name='BACKUP_SIGNING_PRIVATE_KEY',
        )
    )
    derived_public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    configured_public_key = _decode_ed25519_key(
        public_key_value,
        name='BACKUP_SIGNING_PUBLIC_KEY',
    )
    if derived_public_key != configured_public_key:
        raise BackupConfigError('Backup signing private/public keys do not match')


def verify_manifest_signature(manifest: dict, public_key_value: str) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    validate_manifest(manifest)
    unsigned = dict(manifest)
    try:
        signature = base64.b64decode(unsigned.pop('signature'), validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(
            _decode_ed25519_key(
                public_key_value,
                name='BACKUP_SIGNING_PUBLIC_KEY',
            )
        )
        public_key.verify(signature, json_bytes(unsigned))
    except InvalidSignature as exc:
        raise BackupConfigError('Manifest signature verification failed') from exc


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise BackupConfigError('Timestamp must include timezone')
    return parsed.astimezone(dt.UTC)


@dataclass(frozen=True)
class S3Settings:
    bucket: str
    prefix: str
    endpoint_url: str
    region: str
    access_key: str
    secret_key: str
    session_token: str | None

    @classmethod
    def from_env(cls, prefix: str = 'BACKUP'):
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', prefix):
            raise BackupConfigError('S3 environment prefix is invalid')
        return cls(
            bucket=required_env(f'{prefix}_S3_BUCKET'),
            prefix=os.environ.get(f'{prefix}_S3_PREFIX', 'postgres').strip('/'),
            endpoint_url=required_env(f'{prefix}_S3_ENDPOINT'),
            region=os.environ.get(f'{prefix}_S3_REGION', 'ru-central1'),
            access_key=required_env(f'{prefix}_S3_ACCESS_KEY'),
            secret_key=required_env(f'{prefix}_S3_SECRET_KEY'),
            session_token=os.environ.get(f'{prefix}_S3_SESSION_TOKEN') or None,
        )

    def client(self):
        import boto3
        from botocore.config import Config

        return boto3.client(
            's3',
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            aws_session_token=self.session_token,
            config=Config(
                connect_timeout=10,
                read_timeout=120,
                retries={'max_attempts': 5, 'mode': 'standard'},
            ),
        )

    def key(self, suffix: str) -> str:
        return f'{self.prefix}/{suffix}' if self.prefix else suffix


@contextlib.contextmanager
def exclusive_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('w') as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupConfigError('Another backup process is already running') from exc
        yield


def notify_monitor(url: str | None, payload: dict) -> None:
    if not url:
        return
    request = urllib.request.Request(
        url,
        data=json_bytes(payload),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status >= 300:
            raise RuntimeError(f'Monitor returned HTTP {response.status}')


def notify_monitor_safely(url: str | None, payload: dict, logger) -> None:
    try:
        notify_monitor(url, payload)
    except Exception:
        logger.exception('Monitor notification failed')
