import os

from apps.core.healthchecks import celery_beat_heartbeat_is_fresh, main


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
