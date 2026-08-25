import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_development_env_example_keeps_optional_s3_storage_disabled():
    values = {
        key: value
        for line in (ROOT / '.env.example').read_text().splitlines()
        if line and not line.startswith('#') and '=' in line
        for key, value in [line.split('=', 1)]
    }

    assert values['DJANGO_SETTINGS_MODULE'] == 'config.settings.development'
    assert values['YC_S3_BUCKET'] == ''
    assert values['YC_S3_ACCESS_KEY'] == ''
    assert values['YC_S3_SECRET_KEY'] == ''
    assert values['PUBLIC_HTTP_PROXY_URL'] == ''
    development_settings = (ROOT / 'config/settings/development.py').read_text()
    assert "PUBLIC_HTTP_PROXY_URL = ''" in development_settings


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
        'FIELD_ENCRYPTION_KEY': 'Y2ktb25seS1mZXJuZXQta2V5LTMyLWJ5dGVzISEhISE=',
        'FIELD_ENCRYPTION_KEYS': 'Y2ktb25seS1mZXJuZXQta2V5LTMyLWJ5dGVzISEhISE=',
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
        'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED': 'false',
        'MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS': '3600',
        'MARKETPLACE_FEED_STORAGE_MODE': 'legacy_public',
    }


@pytest.mark.parametrize('value', ('', 'enabled', 'true', 'DUAL-WRITE'))
def test_production_requires_explicit_valid_avito_lifecycle_mode(value):
    environment = _production_environment()
    environment['AVITO_STATUS_LIFECYCLE_MODE'] = value

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'AVITO_STATUS_LIFECYCLE_MODE' in result.stderr


@pytest.mark.parametrize('value', ('legacy', 'dual_write', ' DUAL_WRITE '))
def test_production_accepts_explicit_avito_lifecycle_modes(value):
    environment = _production_environment()
    environment['AVITO_STATUS_LIFECYCLE_MODE'] = value

    command = (
        'import config.settings.production as settings; '
        'assert settings.AVITO_STATUS_LIFECYCLE_MODE in {"legacy", "dual_write"}'
    )
    result = subprocess.run(
        [sys.executable, '-c', command],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('value', ('', 'enabled', 'true', 'dual_write'))
def test_production_requires_explicit_valid_marketplace_feed_run_mode(value):
    environment = _production_environment()
    environment['MARKETPLACE_FEED_RUN_MODE'] = value

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_RUN_MODE' in result.stderr


@pytest.mark.parametrize('value', ('legacy', ' LEGACY '))
def test_production_accepts_explicit_legacy_marketplace_feed_run_mode(value):
    environment = _production_environment()
    environment['MARKETPLACE_FEED_RUN_MODE'] = value

    result = subprocess.run(
        [
            sys.executable,
            '-c',
            'import config.settings.production as settings; '
            'assert settings.MARKETPLACE_FEED_RUN_MODE == "legacy"',
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_production_durable_feed_run_requires_dual_write_lifecycle():
    environment = _production_environment()
    environment['MARKETPLACE_FEED_RUN_MODE'] = 'durable'

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'AVITO_STATUS_LIFECYCLE_MODE=dual_write' in result.stderr


def test_production_accepts_durable_feed_run_with_dual_write_lifecycle():
    environment = _production_environment()
    environment['MARKETPLACE_FEED_RUN_MODE'] = 'durable'
    environment['AVITO_STATUS_LIFECYCLE_MODE'] = 'dual_write'

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('value', ('', 'durable', 'enabled'))
def test_p5_production_rejects_unknown_feed_ingress_mode(value):
    environment = _production_environment()
    environment['MARKETPLACE_FEED_INGRESS_MODE'] = value

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_INGRESS_MODE' in result.stderr


@pytest.mark.parametrize('value', ('legacy', 'dual_write'))
def test_p5_production_accepts_explicit_feed_ingress_modes(value):
    environment = _production_environment()
    environment['MARKETPLACE_FEED_INGRESS_MODE'] = f' {value.upper()} '
    if value == 'dual_write':
        environment['AVITO_STATUS_LIFECYCLE_MODE'] = 'dual_write'

    result = subprocess.run(
        [
            sys.executable,
            '-c',
            'import config.settings.production as settings; '
            f'assert settings.MARKETPLACE_FEED_INGRESS_MODE == "{value}"',
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_p5_production_dual_write_ingress_requires_dual_write_lifecycle():
    environment = _production_environment()
    environment['MARKETPLACE_FEED_INGRESS_MODE'] = 'dual_write'

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'AVITO_STATUS_LIFECYCLE_MODE=dual_write' in result.stderr


@pytest.mark.parametrize('value', ('', 'enabled', '1', 'yes'))
def test_production_requires_explicit_feed_profile_migration_boolean(value):
    environment = _production_environment()
    environment['MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED'] = value

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED' in result.stderr


@pytest.mark.parametrize('value', ('', '299', '86401', '-1', '3.5', '٣٦٠٠'))
def test_production_rejects_invalid_feed_profile_source_settlement(value):
    environment = _production_environment()
    environment['MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS'] = value

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS' in result.stderr


@pytest.mark.parametrize('value', ('300', '3600', '86400'))
def test_production_accepts_bounded_feed_profile_source_settlement(value):
    environment = _production_environment()
    environment['MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS'] = value

    command = (
        'import config.settings.production as settings; '
        'assert settings.MARKETPLACE_FEED_PROFILE_SOURCE_SETTLEMENT_SECONDS '
        f'== {value}'
    )
    result = subprocess.run(
        [sys.executable, '-c', command],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def _stable_bridge_environment() -> dict[str, str]:
    environment = _production_environment()
    environment.update({
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


def test_production_feed_profile_migration_requires_stable_bridge_contract():
    environment = _production_environment()
    environment['MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED'] = 'true'

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_STORAGE_MODE=stable_bridge' in result.stderr


def test_production_accepts_profile_migration_with_bridge_contract():
    environment = _stable_bridge_environment()
    environment['MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED'] = 'true'

    result = subprocess.run(
        [
            sys.executable,
            '-c',
            'import config.settings.production as settings; '
            'assert settings.MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED is True; '
            'assert len(settings.MARKETPLACE_FEED_URL_SIGNING_KEYS["feed-v1"]) == 32',
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('value', ('', 'public', 'private_generation', 'true'))
def test_production_rejects_unavailable_marketplace_feed_storage_modes(value):
    environment = _production_environment()
    environment['MARKETPLACE_FEED_STORAGE_MODE'] = value

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'MARKETPLACE_FEED_STORAGE_MODE' in result.stderr


@pytest.mark.parametrize(
    ('name', 'value'),
    (
        ('MARKETPLACE_FEED_PUBLIC_BASE_URL', 'http://api.example.test/marketplace-feeds/v1/feed.xml'),
        ('MARKETPLACE_FEED_PUBLIC_BASE_URL', 'https://api.example.test/other.xml'),
        ('MARKETPLACE_FEED_PUBLIC_BASE_URL', 'https://api.example.test:8443/marketplace-feeds/v1/feed.xml'),
        ('MARKETPLACE_FEED_PUBLIC_BASE_URL', 'https://feeds.example.test/marketplace-feeds/v1/feed.xml'),
        ('MARKETPLACE_FEED_URL_SIGNING_KEYS', '{"feed-v1":"c2hvcnQ"}'),
        ('MARKETPLACE_FEED_URL_SIGNING_PRIMARY_KEY_ID', 'missing'),
        ('YC_CDN_DOMAIN', 'trusted.example.test@attacker.example'),
    ),
)
def test_production_stable_bridge_fails_closed_on_invalid_contract(name, value):
    environment = _stable_bridge_environment()
    environment[name] = value

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert name in result.stderr


def test_production_allows_explicitly_disabled_billing_without_credentials():
    environment = _production_environment()
    environment['BILLING_ENABLED'] = 'false'
    environment.pop('YOOKASSA_SHOP_ID')
    environment.pop('YOOKASSA_SECRET_KEY')

    command = (
        'import config.settings.production as settings; '
        'assert not settings.BILLING_ENABLED'
    )
    result = subprocess.run(
        [sys.executable, '-c', command],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_production_public_preflight_uses_independent_storage_infrastructure():
    environment = _production_environment()
    environment['PUBLIC_HTTP_PREFLIGHT_URL'] = (
        'https://attacker.example.test/ignored'
    )
    command = (
        'import config.settings.production as settings; '
        'assert settings.PUBLIC_HTTP_PREFLIGHT_URL == '
        '"https://storage.yandexcloud.net/"'
    )

    result = subprocess.run(
        [sys.executable, '-c', command],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_production_requires_site_hostname_in_allowed_hosts():
    environment = _production_environment()
    environment['ALLOWED_HOSTS'] = 'other.example.test'

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'Hostname из SITE_URL должен быть разрешён' in result.stderr


@pytest.mark.parametrize('missing_name', ('YOOKASSA_SHOP_ID', 'YOOKASSA_SECRET_KEY'))
def test_production_enabled_billing_requires_provider_credentials(missing_name):
    environment = _production_environment()
    environment.pop(missing_name)

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert f'{missing_name} обязателен в production.' in result.stderr


@pytest.mark.parametrize('value', ('', 'yes', '0', 'disabled'))
def test_production_requires_explicit_boolean_billing_switch(value):
    environment = _production_environment()
    environment['BILLING_ENABLED'] = value

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'BILLING_ENABLED' in result.stderr


@pytest.mark.parametrize(
    'missing_name',
    ('YC_S3_BUCKET', 'YC_S3_ACCESS_KEY', 'YC_S3_SECRET_KEY'),
)
def test_production_requires_complete_media_storage_credentials(missing_name):
    environment = _production_environment()
    environment.pop(missing_name)

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert f'{missing_name} обязателен в production.' in result.stderr


def test_production_media_storage_uses_required_s3_credentials():
    assertions = """
from config.settings import production as settings
storage = settings.STORAGES['default']
assert storage['BACKEND'] == 'storages.backends.s3boto3.S3Boto3Storage'
assert storage['OPTIONS']['bucket_name'] == 'media-production'
assert storage['OPTIONS']['access_key'] == 'media-access'
assert storage['OPTIONS']['secret_key'] == 'media-secret'
"""

    result = subprocess.run(
        [sys.executable, '-c', assertions],
        cwd=ROOT,
        env=_production_environment(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_production_media_storage_uses_the_validated_trimmed_values():
    environment = _production_environment()
    environment.update({
        'YC_S3_BUCKET': '  media-production  ',
        'YC_S3_ACCESS_KEY': '  media-access  ',
        'YC_S3_SECRET_KEY': '  media-secret  ',
    })
    assertions = """
from config.settings import production as settings
options = settings.STORAGES['default']['OPTIONS']
assert settings.YC_S3_BUCKET == options['bucket_name'] == 'media-production'
assert settings.YC_S3_ACCESS_KEY == options['access_key'] == 'media-access'
assert settings.YC_S3_SECRET_KEY == options['secret_key'] == 'media-secret'
"""

    result = subprocess.run(
        [sys.executable, '-c', assertions],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ('password_name', 'wrong_value'),
    [
        ('CACHE_REDIS_PASSWORD', 'wrong-cache-secret'),
        ('CELERY_REDIS_PASSWORD', 'wrong-durable-secret'),
    ],
)
def test_production_rejects_redis_url_and_server_password_mismatch(
    password_name,
    wrong_value,
):
    environment = _production_environment()
    environment[password_name] = wrong_value

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert password_name in result.stderr


@pytest.mark.parametrize(
    'missing_name',
    (
        'CORS_ALLOWED_ORIGINS',
        'RESEND_API_KEY',
        'DEFAULT_FROM_EMAIL',
        'EMAIL_HTTP_PROXY_URL',
    ),
)
def test_production_requires_complete_smtp_and_browser_origin_settings(
    missing_name,
):
    environment = _production_environment()
    environment.pop(missing_name)

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert f'{missing_name} обязателен в production.' in result.stderr


@pytest.mark.parametrize(
    'setting_name',
    (
        'CORS_ALLOWED_ORIGINS',
        'CSRF_TRUSTED_ORIGINS',
        'SITE_URL',
        'FRONTEND_URL',
    ),
)
@pytest.mark.parametrize(
    'invalid_origin',
    (
        'http://app.example.test',
        'https://user:secret@app.example.test',
        'https://app.example.test/path',
        'https://app.example.test?query=value',
        'https://app.example.test#fragment',
        'https://app.example.test/',
        'https://app.example.test\\path',
        'https://app.example.test%2Fpath',
    ),
)
def test_production_rejects_non_origin_urls(setting_name, invalid_origin):
    environment = _production_environment()
    environment[setting_name] = invalid_origin

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert setting_name in result.stderr


@pytest.mark.parametrize(
    'setting_name',
    (
        'CORS_ALLOWED_ORIGINS',
        'CSRF_TRUSTED_ORIGINS',
        'BILLING_RETURN_URL_ALLOWED_ORIGINS',
    ),
)
def test_production_requires_frontend_in_browser_and_checkout_allowlists(
    setting_name,
):
    environment = _production_environment()
    environment[setting_name] = 'https://other.example.test'

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert f'{setting_name} должен включать FRONTEND_URL.' in result.stderr


@pytest.mark.parametrize(
    ('setting_name', 'invalid_value'),
    (
        ('RESEND_API_KEY', 'not-a-resend-key'),
        ('EMAIL_HTTP_PROXY_URL', 'http://attacker.example:3128'),
        ('EMAIL_HTTP_PROXY_URL', 'http://user:secret@egress_proxy:3128'),
    ),
)
def test_production_rejects_invalid_smtp_credentials_or_proxy(
    setting_name,
    invalid_value,
):
    environment = _production_environment()
    environment[setting_name] = invalid_value

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert setting_name in result.stderr


@pytest.mark.parametrize(
    'invalid_sender',
    (
        'not-an-email',
        'MAP <noreply@example.test>',
        'noreply@example.test\nBcc: attacker@example.test',
        'noreply@dodugir.com',
        'noreply@tenant.example',
    ),
)
def test_production_rejects_invalid_default_from_email(invalid_sender):
    environment = _production_environment()
    environment['DEFAULT_FROM_EMAIL'] = invalid_sender

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'DEFAULT_FROM_EMAIL' in result.stderr


def test_production_uses_fixed_starttls_backend_and_bounded_timeout():
    assertions = """
from config.settings import production as settings
assert settings.EMAIL_BACKEND == 'apps.core.email_backend.HTTPProxySMTPEmailBackend'
assert settings.EMAIL_HOST == 'smtp.resend.com'
assert settings.EMAIL_PORT == 587
assert settings.EMAIL_USE_TLS is True
assert settings.EMAIL_USE_SSL is False
assert settings.EMAIL_TIMEOUT == 10
assert settings.EMAIL_HOST_USER == 'resend'
assert settings.EMAIL_HTTP_PROXY_URL == 'http://egress_proxy:3128'
"""

    result = subprocess.run(
        [sys.executable, '-c', assertions],
        cwd=ROOT,
        env=_production_environment(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    'invalid_proxy',
    (
        '',
        'http://attacker.example:3128',
        'http://user:secret@egress_proxy:3128',
        'https://egress_proxy:3128',
    ),
)
def test_production_requires_the_exact_public_http_proxy(invalid_proxy):
    environment = _production_environment()
    environment['PUBLIC_HTTP_PROXY_URL'] = invalid_proxy

    result = subprocess.run(
        [sys.executable, '-c', 'import config.settings.production'],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode != 0
    assert 'PUBLIC_HTTP_PROXY_URL' in result.stderr


def test_production_uses_the_exact_public_http_proxy():
    assertions = """
from config.settings import production as settings
assert settings.PUBLIC_HTTP_PROXY_URL == 'http://egress_proxy:3128'
"""

    result = subprocess.run(
        [sys.executable, '-c', assertions],
        cwd=ROOT,
        env=_production_environment(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
