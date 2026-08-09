from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / 'docker-compose.prod.yml').read_text())


def test_every_long_running_service_has_restart_and_log_rotation():
    services = COMPOSE['services']
    assert services

    for name, service in services.items():
        assert service.get('restart') == 'unless-stopped', name
        logging = service.get('logging', {})
        assert logging.get('driver') == 'json-file', name
        assert logging.get('options', {}).get('max-size') == '10m', name
        assert logging.get('options', {}).get('max-file') == '5', name
        assert service.get('stop_grace_period'), name


def test_runtime_healthchecks_cover_http_workers_beat_and_proxy():
    services = COMPOSE['services']

    assert '/api/v1/live/' in ' '.join(services['django']['healthcheck']['test'])
    assert 'worker-main@' in services['celery_worker']['command']
    assert 'worker-main@' in ' '.join(services['celery_worker']['healthcheck']['test'])
    assert 'worker-images@' in ' '.join(
        services['celery_worker_images']['healthcheck']['test']
    )
    assert 'HeartbeatDatabaseScheduler' in services['celery_beat']['command']
    assert services['celery_beat']['healthcheck']['test'][-1] == 'celery-beat'
    assert '/nginx-health' in ' '.join(services['nginx']['healthcheck']['test'])
