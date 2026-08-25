import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_SETTING_NAMES = (
    'MARKETPLACE_FEED_ARTIFACT_MODE',
    'MARKETPLACE_FEED_ARTIFACT_BUCKET',
    'MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID',
    'MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY',
    'MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER',
    'MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID',
    'MARKETPLACE_FEED_ARTIFACT_MAX_BYTES',
    'MARKETPLACE_FEED_REDIRECT_TTL_SECONDS',
)


def _base_environment() -> dict[str, str]:
    environment = {
        **os.environ,
        'DJANGO_SETTINGS_MODULE': 'config.settings.base',
    }
    for name in ARTIFACT_SETTING_NAMES:
        environment.pop(name, None)
    return environment


def _production_environment() -> dict[str, str]:
    return {
        **os.environ,
        'DJANGO_SECRET_KEY': 's' * 64,
        'ALLOWED_HOSTS': 'api.example.test',
        'CORS_ALLOWED_ORIGINS': 'https://app.example.test',
        'CSRF_TRUSTED_ORIGINS': 'https://app.example.test',
        'DATABASE_URL': 'postgresql://app:secret@database:5432/app',
        'CACHE_REDIS_PASSWORD': 'cache-secret',
        'CELERY_REDIS_PASSWORD': 'durable-secret',
        'CACHE_REDIS_URL': 'redis://:cache-secret@cache:6379/0',
        'CELERY_BROKER_URL': 'redis://:durable-secret@broker:6379/0',
        'CELERY_RESULT_BACKEND': 'redis://:durable-secret@broker:6379/1',
        'COORDINATION_REDIS_URL': 'redis://:durable-secret@broker:6379/2',
        'FIELD_ENCRYPTION_KEY': (
            'Y2ktb25seS1mZXJuZXQta2V5LTMyLWJ5dGVzISEhISE='
        ),
        'FIELD_ENCRYPTION_KEYS': (
            'Y2ktb25seS1mZXJuZXQta2V5LTMyLWJ5dGVzISEhISE='
        ),
        'SITE_URL': 'https://api.example.test',
        'FRONTEND_URL': 'https://app.example.test',
        'BILLING_RETURN_URL_ALLOWED_ORIGINS': 'https://app.example.test',
        'BILLING_ENABLED': 'true',
        'YOOKASSA_SHOP_ID': 'shop-id',
        'YOOKASSA_SECRET_KEY': 'payment-secret',
        'YOOKASSA_ALLOW_TEST_PAYMENTS': 'false',
        'YC_S3_BUCKET': 'media-production',
        'YC_S3_ACCESS_KEY': 'media-access',
        'YC_S3_SECRET_KEY': 'media-secret',
        'RESEND_API_KEY': 're_ci-only-provider-secret',
        'DEFAULT_FROM_EMAIL': 'noreply@notify.dodugir.com',
        'EMAIL_HTTP_PROXY_URL': 'http://egress_proxy:3128',
        'PUBLIC_HTTP_PROXY_URL': 'http://egress_proxy:3128',
        'AVITO_STATUS_LIFECYCLE_MODE': 'legacy',
        'MARKETPLACE_FEED_RUN_MODE': 'legacy',
        'MARKETPLACE_FEED_INGRESS_MODE': 'legacy',
        'MARKETPLACE_FEED_ARTIFACT_MODE': 'disabled',
        'MARKETPLACE_FEED_ARTIFACT_BUCKET': '',
        'MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID': '',
        'MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY': '',
        'MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER': '',
        'MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID': '',
        'MARKETPLACE_FEED_ARTIFACT_MAX_BYTES': '268435456',
        'MARKETPLACE_FEED_REDIRECT_TTL_SECONDS': '120',
        'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED': 'false',
        'MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS': '3600',
        'MARKETPLACE_FEED_STORAGE_MODE': 'legacy_public',
    }


def _settings_command(module: str) -> str:
    return (
        'import json; '
        f'import {module} as settings; '
        'print(json.dumps({'
        '"mode": settings.MARKETPLACE_FEED_ARTIFACT_MODE, '
        '"bucket": settings.MARKETPLACE_FEED_ARTIFACT_BUCKET, '
        '"max_bytes": settings.MARKETPLACE_FEED_ARTIFACT_MAX_BYTES, '
        '"ttl": settings.MARKETPLACE_FEED_REDIRECT_TTL_SECONDS}))'
    )


def _run_settings(module: str, environment: dict[str, str], command: str | None = None):
    return subprocess.run(
        [sys.executable, '-c', command or _settings_command(module)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def _result_json(result) -> dict:
    return json.loads(result.stdout.strip().splitlines()[-1])


def _stable_shadow_environment() -> dict[str, str]:
    environment = _production_environment()
    environment.update({
        'AVITO_STATUS_LIFECYCLE_MODE': 'dual_write',
        'MARKETPLACE_FEED_ARTIFACT_MODE': 'shadow',
        'MARKETPLACE_FEED_ARTIFACT_BUCKET': 'private-feed-artifacts-1',
        'MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID': 'private-access',
        'MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY': 'private-secret',
        'MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER': 'folder-owner-1',
        'MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID': 'kms-key-1',
        'MARKETPLACE_FEED_INGRESS_MODE': 'dual_write',
        'MARKETPLACE_FEED_STORAGE_MODE': 'stable_bridge',
        'MARKETPLACE_FEED_PUBLIC_BASE_URL': (
            'https://api.example.test/marketplace-feeds/v1/feed.xml'
        ),
        'MARKETPLACE_FEED_URL_SIGNING_KEYS': (
            '{"feed-v1":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"}'
        ),
        'MARKETPLACE_FEED_URL_SIGNING_PRIMARY_KEY_ID': 'feed-v1',
    })
    return environment


def _private_canary_environment() -> dict[str, str]:
    environment = _stable_shadow_environment()
    environment.update({
        'MARKETPLACE_FEED_ARTIFACT_MODE': 'canary',
        'MARKETPLACE_FEED_STORAGE_MODE': 'private_generation',
    })
    return environment


def test_base_artifact_settings_are_dark_and_bounded_by_default():
    result = _run_settings('config.settings.base', _base_environment())

    assert result.returncode == 0, result.stderr
    assert _result_json(result) == {
        'mode': 'disabled',
        'bucket': '',
        'max_bytes': 268_435_456,
        'ttl': 120,
    }


@pytest.mark.parametrize('mode', ('disabled', 'shadow', 'canary', 'active'))
def test_base_accepts_every_rollout_mode(mode):
    environment = _base_environment()
    environment['MARKETPLACE_FEED_ARTIFACT_MODE'] = mode
    environment['MARKETPLACE_FEED_ARTIFACT_BUCKET'] = (
        '' if mode == 'disabled' else 'private-feed-artifacts-1'
    )
    if mode != 'disabled':
        environment.update({
            'MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID': 'private-access',
            'MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY': 'private-secret',
            'MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER': 'folder-owner-1',
            'MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID': 'kms-key-1',
        })

    result = _run_settings('config.settings.base', environment)

    assert result.returncode == 0, result.stderr
    assert _result_json(result)['mode'] == mode


@pytest.mark.parametrize('mode', ('', 'enabled', 'dual_write', 'private'))
def test_base_rejects_unknown_artifact_mode(mode):
    environment = _base_environment()
    environment['MARKETPLACE_FEED_ARTIFACT_MODE'] = mode

    result = _run_settings('config.settings.base', environment)

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_ARTIFACT_MODE' in result.stderr


@pytest.mark.parametrize(
    ('name', 'valid_values'),
    (
        ('MARKETPLACE_FEED_ARTIFACT_MAX_BYTES', ('1', '1073741824')),
        ('MARKETPLACE_FEED_REDIRECT_TTL_SECONDS', ('30', '300')),
    ),
)
def test_base_accepts_exact_numeric_boundaries(name, valid_values):
    for value in valid_values:
        environment = _base_environment()
        environment[name] = value

        result = _run_settings('config.settings.base', environment)

        assert result.returncode == 0, (name, value, result.stderr)


@pytest.mark.parametrize(
    ('name', 'invalid_values'),
    (
        (
            'MARKETPLACE_FEED_ARTIFACT_MAX_BYTES',
            ('', '0', '1073741825', '+1', '1.0', '١'),
        ),
        (
            'MARKETPLACE_FEED_REDIRECT_TTL_SECONDS',
            ('', '29', '301', '+30', '30.0', '٣٠'),
        ),
    ),
)
def test_base_rejects_non_ascii_or_out_of_range_numbers(name, invalid_values):
    for value in invalid_values:
        environment = _base_environment()
        environment[name] = value

        result = _run_settings('config.settings.base', environment)

        assert result.returncode != 0, (name, value)
        assert name in result.stderr


@pytest.mark.parametrize('mode', ('shadow', 'canary', 'active'))
def test_base_requires_bucket_for_every_non_disabled_mode(mode):
    environment = _base_environment()
    environment['MARKETPLACE_FEED_ARTIFACT_MODE'] = mode
    environment['MARKETPLACE_FEED_ARTIFACT_BUCKET'] = ''

    result = _run_settings('config.settings.base', environment)

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_ARTIFACT_BUCKET' in result.stderr


def test_production_allows_explicit_disabled_mode_with_blank_bucket():
    result = _run_settings(
        'config.settings.production',
        _production_environment(),
    )

    assert result.returncode == 0, result.stderr
    assert _result_json(result)['mode'] == 'disabled'
    assert _result_json(result)['bucket'] == ''


@pytest.mark.parametrize('name', ARTIFACT_SETTING_NAMES)
def test_production_requires_every_artifact_setting_to_be_explicit(name):
    environment = _production_environment()
    environment.pop(name)

    result = _run_settings('config.settings.production', environment)

    assert result.returncode != 0
    assert name in result.stderr


@pytest.mark.parametrize(
    ('name', 'invalid_values'),
    (
        ('MARKETPLACE_FEED_ARTIFACT_MAX_BYTES', ('0', '1073741825', '+1', '١')),
        ('MARKETPLACE_FEED_REDIRECT_TTL_SECONDS', ('29', '301', '+30', '٣٠')),
    ),
)
def test_production_rejects_invalid_numeric_contract(name, invalid_values):
    for value in invalid_values:
        environment = _production_environment()
        environment[name] = value

        result = _run_settings('config.settings.production', environment)

        assert result.returncode != 0, (name, value)
        assert name in result.stderr


@pytest.mark.parametrize(
    ('name', 'value'),
    (
        ('MARKETPLACE_FEED_ARTIFACT_MAX_BYTES', '1'),
        ('MARKETPLACE_FEED_ARTIFACT_MAX_BYTES', '1073741824'),
        ('MARKETPLACE_FEED_REDIRECT_TTL_SECONDS', '30'),
        ('MARKETPLACE_FEED_REDIRECT_TTL_SECONDS', '300'),
    ),
)
def test_production_accepts_exact_numeric_boundaries(name, value):
    environment = _production_environment()
    environment[name] = value

    result = _run_settings('config.settings.production', environment)

    assert result.returncode == 0, result.stderr


def test_production_rejects_unknown_artifact_mode():
    environment = _production_environment()
    environment['MARKETPLACE_FEED_ARTIFACT_MODE'] = 'enabled'

    result = _run_settings('config.settings.production', environment)

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_ARTIFACT_MODE' in result.stderr


@pytest.mark.parametrize(
    'bucket',
    (
        'ab',
        'Uppercase-bucket',
        'bucket_name',
        'bucket/name',
        'bucket..name',
        'bucket.-name',
        '-bucket-name',
        'bucket-name-',
        '192.168.1.1',
    ),
)
def test_production_rejects_unsafe_bucket_even_while_disabled(bucket):
    environment = _production_environment()
    environment['MARKETPLACE_FEED_ARTIFACT_BUCKET'] = bucket

    result = _run_settings('config.settings.production', environment)

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_ARTIFACT_BUCKET' in result.stderr


@pytest.mark.parametrize('mode', ('shadow', 'canary'))
def test_production_non_disabled_mode_never_accepts_blank_bucket(mode):
    environment = _production_environment()
    environment['MARKETPLACE_FEED_ARTIFACT_MODE'] = mode
    environment['MARKETPLACE_FEED_ARTIFACT_BUCKET'] = ''

    result = _run_settings('config.settings.production', environment)

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_ARTIFACT_BUCKET' in result.stderr


def test_production_keeps_active_hard_disabled():
    environment = _private_canary_environment()
    environment['MARKETPLACE_FEED_ARTIFACT_MODE'] = 'active'

    result = _run_settings('config.settings.production', environment)

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_ARTIFACT_MODE=active' in result.stderr
    assert 'P6 canary' in result.stderr


def test_production_accepts_bounded_private_canary_contract():
    result = _run_settings(
        'config.settings.production',
        _private_canary_environment(),
    )

    assert result.returncode == 0, result.stderr
    assert _result_json(result)['mode'] == 'canary'


@pytest.mark.parametrize(
    ('name', 'value'),
    (
        ('MARKETPLACE_FEED_STORAGE_MODE', 'stable_bridge'),
        ('MARKETPLACE_FEED_INGRESS_MODE', 'legacy'),
        ('MARKETPLACE_FEED_RUN_MODE', 'durable'),
        ('MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED', 'true'),
    ),
)
def test_production_canary_rejects_broader_rollout(name, value):
    environment = _private_canary_environment()
    environment[name] = value

    result = _run_settings('config.settings.production', environment)

    assert result.returncode != 0
    assert 'P6 canary' in result.stderr


def test_production_accepts_shadow_only_with_stable_bridge_and_dual_write():
    result = _run_settings(
        'config.settings.production',
        _stable_shadow_environment(),
    )

    assert result.returncode == 0, result.stderr
    assert _result_json(result)['mode'] == 'shadow'


@pytest.mark.parametrize(
    ('name', 'value'),
    (
        ('MARKETPLACE_FEED_STORAGE_MODE', 'legacy_public'),
        ('MARKETPLACE_FEED_INGRESS_MODE', 'legacy'),
    ),
)
def test_production_shadow_rejects_missing_rollout_dependency(name, value):
    environment = _stable_shadow_environment()
    environment[name] = value

    result = _run_settings('config.settings.production', environment)

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_ARTIFACT_MODE=shadow' in result.stderr
    assert name in result.stderr


def test_production_shadow_import_performs_no_network_preflight():
    command = '''
from unittest.mock import patch

forbidden = AssertionError("settings import attempted network I/O")
with (
    patch("socket.create_connection", side_effect=forbidden),
    patch("socket.getaddrinfo", side_effect=forbidden),
):
    import config.settings.production as settings
    assert settings.MARKETPLACE_FEED_ARTIFACT_MODE == "shadow"
'''

    result = _run_settings(
        'config.settings.production',
        _stable_shadow_environment(),
        command=command,
    )

    assert result.returncode == 0, result.stderr


def test_env_example_documents_complete_dark_artifact_contract():
    values = {
        key: value
        for line in (ROOT / '.env.example').read_text().splitlines()
        if line and not line.startswith('#') and '=' in line
        for key, value in [line.split('=', 1)]
    }
    assert values['MARKETPLACE_FEED_ARTIFACT_MODE'] == 'disabled'
    assert values['MARKETPLACE_FEED_ARTIFACT_BUCKET'] == ''
    assert values['MARKETPLACE_FEED_ARTIFACT_ACCESS_KEY_ID'] == ''
    assert values['MARKETPLACE_FEED_ARTIFACT_SECRET_ACCESS_KEY'] == ''
    assert values['MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER'] == ''
    assert values['MARKETPLACE_FEED_ARTIFACT_KMS_KEY_ID'] == ''
    assert values['MARKETPLACE_FEED_ARTIFACT_MAX_BYTES'] == '268435456'
    assert values['MARKETPLACE_FEED_REDIRECT_TTL_SECONDS'] == '120'
