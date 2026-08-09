import os
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
import pytest
from psycopg import sql

from backup.common import (
    collect_database_metadata,
    collect_row_counts,
    collect_snapshot_database_metadata,
    collect_snapshot_row_counts,
    exported_snapshot,
    parse_database_url,
    postgres_environment,
    postgres_server_major,
    psql_scalar,
    run_checked,
    tool_major_version,
)
from backup.restore import _assert_empty_database


SOURCE_URL = os.environ.get('BACKUP_INTEGRATION_DATABASE_URL')
pytestmark = pytest.mark.skipif(
    not SOURCE_URL,
    reason='BACKUP_INTEGRATION_DATABASE_URL is not configured',
)


def _replace_database(database_url: str, database: str) -> str:
    parsed = urlsplit(database_url)
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        f'/{quote(database, safe="")}',
        parsed.query,
        '',
    ))


def _target_url(source_url: str, role: str, password: str, database: str) -> str:
    parsed = urlsplit(source_url)
    host = parsed.hostname
    if ':' in host:
        host = f'[{host}]'
    netloc = f'{quote(role, safe="")}:{quote(password, safe="")}@{host}'
    if parsed.port:
        netloc += f':{parsed.port}'
    return urlunsplit((
        parsed.scheme,
        netloc,
        f'/{quote(database, safe="")}',
        parsed.query,
        '',
    ))


def test_consistent_snapshot_restores_into_safe_empty_database():
    suffix = uuid.uuid4().hex[:10]
    backup_role_name = f'backup_source_{suffix}'
    role_name = f'backup_drill_{suffix}'
    database_name = f'backup_drill_{suffix}'
    backup_role_password = uuid.uuid4().hex
    role_password = uuid.uuid4().hex
    source_url = SOURCE_URL
    source_database = parse_database_url(
        'BACKUP_INTEGRATION_DATABASE_URL',
        source_url,
    ).database
    backup_url = _target_url(
        source_url,
        backup_role_name,
        backup_role_password,
        source_database,
    )
    source_target = parse_database_url('BACKUP_DATABASE_URL', backup_url)
    admin_url = _replace_database(source_url, 'postgres')
    target_url = _target_url(source_url, role_name, role_password, database_name)

    source_counts = None
    source_metadata = None
    database_created = False
    role_created = False
    backup_role_created = False

    with psycopg.connect(admin_url, autocommit=True) as admin_connection:
        try:
            admin_connection.execute(
                sql.SQL(
                    'CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE'
                ).format(
                    sql.Identifier(backup_role_name),
                    sql.Literal(backup_role_password),
                )
            )
            backup_role_created = True
            admin_connection.execute(
                sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                    sql.Identifier(source_database),
                    sql.Identifier(backup_role_name),
                )
            )
            admin_connection.execute(
                sql.SQL('GRANT pg_read_all_data TO {}').format(
                    sql.Identifier(backup_role_name)
                )
            )
            admin_connection.execute(
                sql.SQL(
                    'CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE'
                ).format(
                    sql.Identifier(role_name),
                    sql.Literal(role_password),
                )
            )
            role_created = True

            with exported_snapshot(backup_url) as (snapshot_connection, snapshot_id):
                source_counts = collect_snapshot_row_counts(snapshot_connection)
                source_metadata = collect_snapshot_database_metadata(snapshot_connection)

                admin_connection.execute(
                    sql.SQL(
                        'CREATE DATABASE {} OWNER {} TEMPLATE template0 '
                        'ENCODING {} LC_COLLATE {} LC_CTYPE {}'
                    ).format(
                        sql.Identifier(database_name),
                        sql.Identifier(role_name),
                        sql.Literal(source_metadata['encoding']),
                        sql.Literal(source_metadata['collate']),
                        sql.Literal(source_metadata['ctype']),
                    )
                )
                database_created = True

                with tempfile.TemporaryDirectory(prefix='backup-integration-') as temp_dir:
                    workdir = Path(temp_dir)
                    dump_file = workdir / 'database.dump'
                    with postgres_environment(backup_url, workdir) as (_, source_env):
                        source_major = postgres_server_major(source_target, source_env)
                        assert tool_major_version('pg_dump') == source_major
                        run_checked([
                            'pg_dump',
                            *source_target.cli_args(),
                            f'--snapshot={snapshot_id}',
                            '--format=custom',
                            '--no-owner',
                            '--no-acl',
                            '--file',
                            str(dump_file),
                        ], env=source_env, timeout=300)

                    with postgres_environment(
                        target_url,
                        workdir,
                        name='RESTORE_DATABASE_URL',
                    ) as (target, target_env):
                        _assert_empty_database(target, target_env)
                        assert collect_database_metadata(target, target_env) == source_metadata
                        assert tool_major_version('pg_restore') == source_major
                        run_checked([
                            'pg_restore',
                            *target.cli_args(),
                            '--exit-on-error',
                            '--single-transaction',
                            '--no-owner',
                            '--no-acl',
                            str(dump_file),
                        ], env=target_env, timeout=300)

                        assert int(psql_scalar(
                            target,
                            target_env,
                            'SELECT count(*) FROM "public"."django_migrations";',
                        )) > 0
                        assert collect_row_counts(target, target_env) == source_counts
        finally:
            if database_created:
                admin_connection.execute(
                    'SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
                    'WHERE datname = %s AND pid <> pg_backend_pid()',
                    (database_name,),
                )
                admin_connection.execute(
                    sql.SQL('DROP DATABASE {}').format(sql.Identifier(database_name))
                )
            if role_created:
                admin_connection.execute(
                    sql.SQL('DROP ROLE {}').format(sql.Identifier(role_name))
                )
            if backup_role_created:
                admin_connection.execute(
                    sql.SQL('REVOKE CONNECT ON DATABASE {} FROM {}').format(
                        sql.Identifier(source_database),
                        sql.Identifier(backup_role_name),
                    )
                )
                admin_connection.execute(
                    sql.SQL('REVOKE pg_read_all_data FROM {}').format(
                        sql.Identifier(backup_role_name)
                    )
                )
                admin_connection.execute(
                    sql.SQL('DROP ROLE {}').format(sql.Identifier(backup_role_name))
                )
