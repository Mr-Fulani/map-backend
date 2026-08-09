import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / 'docker-compose.prod.yml').read_text())
RESTORE_COMPOSE = yaml.safe_load((ROOT / 'docker-compose.restore.yml').read_text())
BACKUP_LIFECYCLE = json.loads(
    (ROOT / 'ops/s3/backup-lifecycle.json').read_text()
)


def test_every_long_running_service_has_restart_and_log_rotation():
    services = COMPOSE['services']
    assert services

    for name, service in services.items():
        if 'ops' in service.get('profiles', []):
            assert service.get('restart') == 'no', name
        else:
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


def test_cache_and_durable_broker_are_separate_services():
    services = COMPOSE['services']
    cache_command = ' '.join(services['redis']['command'])
    broker_command = ' '.join(services['redis_broker']['command'])

    assert 'allkeys-lru' in cache_command
    assert 'appendonly yes' in broker_command
    assert 'appendfsync everysec' in broker_command
    assert 'noeviction' in broker_command
    assert services['redis_broker']['volumes'] == ['redis_broker_data:/data']
    for name in ('django', 'celery_worker', 'celery_beat', 'celery_worker_images'):
        assert services[name]['depends_on']['redis_broker']['condition'] == 'service_healthy'


def test_backup_is_one_shot_isolated_and_persistent_only_for_its_lock():
    backup = COMPOSE['services']['backup']

    assert backup['profiles'] == ['ops']
    assert backup['restart'] == 'no'
    assert backup['build']['dockerfile'] == 'backup/Dockerfile'
    assert backup['env_file'] == ['.backup.env']
    assert backup['volumes'] == ['backup_state:/state']
    assert backup['networks'] == ['backend']
    assert backup['environment']['HTTP_PROXY'] == 'http://egress_proxy:3128'
    assert 'ports' not in backup
    assert COMPOSE['volumes']['backup_state'] is None


def test_restore_runtime_is_separate_ephemeral_and_read_only():
    restore = RESTORE_COMPOSE['services']['restore']
    compose_source = (ROOT / 'docker-compose.restore.yml').read_text()
    wrapper = (ROOT / 'scripts/production_restore.sh').read_text()

    assert 'env_file' not in restore
    assert '.backup.env' not in compose_source
    assert 'BACKUP_SIGNING_PRIVATE_KEY' not in compose_source
    assert restore['depends_on'] == {
        'egress_proxy': {'condition': 'service_started'},
    }
    assert restore['restart'] == 'no'
    assert restore['read_only'] is True
    assert restore['cap_drop'] == ['ALL']
    assert restore['security_opt'] == ['no-new-privileges:true']
    assert restore['volumes'][0] == 'restore_workspace:/tmp'
    assert restore['environment']['RESTORE_AGE_IDENTITY_FILE'].startswith(
        '/run/secrets/'
    )
    assert restore['environment']['HTTP_PROXY'] == 'http://egress_proxy:3128'
    assert 'saas-poster-restore' in wrapper
    assert 'flock -n 9' in wrapper
    assert 'down --volumes' in wrapper
    assert 'restore cleanup failed' in wrapper
    assert 'run --rm restore' in wrapper


def test_versioned_backup_lifecycle_expires_noncurrent_objects():
    rules = BACKUP_LIFECYCLE['Rules']

    assert rules
    for rule in rules:
        assert rule['Status'] == 'Enabled'
        assert rule['NoncurrentVersionExpiration']['NoncurrentDays'] > 0

    by_id = {rule['ID']: rule for rule in rules}
    for retention_class, days in (
        ('daily', 35),
        ('weekly', 100),
        ('monthly', 400),
    ):
        rule = by_id[f'expire-{retention_class}-database-backups']
        assert rule['Expiration']['Days'] == days
        assert rule['NoncurrentVersionExpiration']['NoncurrentDays'] >= days
    coverage = by_id['expire-old-coverage-markers']
    assert (
        coverage['NoncurrentVersionExpiration']['NoncurrentDays']
        >= coverage['Expiration']['Days']
    )
