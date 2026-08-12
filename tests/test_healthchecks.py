import os
from unittest.mock import MagicMock

from apps.core.healthchecks import (
    DJANGO_LIVENESS_HOST,
    DJANGO_LIVENESS_PATH,
    DJANGO_LIVENESS_PORT,
    DJANGO_LIVENESS_TIMEOUT_SECONDS,
    celery_beat_heartbeat_is_fresh,
    django_liveness_is_healthy,
    main,
)


def test_celery_beat_heartbeat_must_exist(monkeypatch, tmp_path):
    heartbeat = tmp_path / 'missing-heartbeat'
    monkeypatch.setenv('CELERY_BEAT_HEARTBEAT_FILE', str(heartbeat))

    assert celery_beat_heartbeat_is_fresh(now=100) is False


def test_celery_beat_heartbeat_must_be_fresh(monkeypatch, tmp_path):
    heartbeat = tmp_path / 'heartbeat'
    heartbeat.touch()
    os.utime(heartbeat, (100, 100))
    monkeypatch.setenv('CELERY_BEAT_HEARTBEAT_FILE', str(heartbeat))
    monkeypatch.setenv('CELERY_BEAT_HEARTBEAT_MAX_AGE_SECONDS', '120')

    assert celery_beat_heartbeat_is_fresh(now=220) is True
    assert celery_beat_heartbeat_is_fresh(now=221) is False
    assert celery_beat_heartbeat_is_fresh(now=99) is False


def test_healthcheck_cli_rejects_unknown_check():
    assert main(['unknown']) == 2


def test_django_liveness_uses_site_hostname(monkeypatch):
    monkeypatch.setenv('SITE_URL', 'https://dodugir.com')
    response = MagicMock(status=200)
    connection = MagicMock()
    connection.getresponse.return_value = response
    connection_factory = MagicMock(return_value=connection)

    assert django_liveness_is_healthy(connection_factory) is True

    connection_factory.assert_called_once_with(
        DJANGO_LIVENESS_HOST,
        DJANGO_LIVENESS_PORT,
        timeout=DJANGO_LIVENESS_TIMEOUT_SECONDS,
    )
    connection.request.assert_called_once_with(
        'GET',
        DJANGO_LIVENESS_PATH,
        headers={'Host': 'dodugir.com'},
    )
    response.read.assert_called_once_with(1024)
    connection.close.assert_called_once_with()


def test_django_liveness_fails_closed_without_site_url(monkeypatch):
    monkeypatch.delenv('SITE_URL', raising=False)
    connection_factory = MagicMock()

    assert django_liveness_is_healthy(connection_factory) is False
    connection_factory.assert_not_called()


def test_django_liveness_rejects_failed_response(monkeypatch):
    monkeypatch.setenv('SITE_URL', 'https://dodugir.com')
    connection = MagicMock()
    connection.getresponse.return_value = MagicMock(status=503)

    assert django_liveness_is_healthy(MagicMock(return_value=connection)) is False
    connection.close.assert_called_once_with()


def test_django_liveness_handles_connection_failure(monkeypatch):
    monkeypatch.setenv('SITE_URL', 'https://dodugir.com')
    connection_factory = MagicMock(side_effect=OSError('connection refused'))

    assert django_liveness_is_healthy(connection_factory) is False


def test_django_liveness_cli_result(monkeypatch):
    monkeypatch.setattr(
        'apps.core.healthchecks.django_liveness_is_healthy',
        lambda: True,
    )

    assert main(['django-liveness']) == 0
