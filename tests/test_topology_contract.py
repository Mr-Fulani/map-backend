from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'scripts' / 'verify_production_topology.sh').read_text()


def test_topology_check_pins_project_and_compose_file():
    assert '--project-name saas_poster' in SCRIPT
    assert '-f "$ROOT_DIR/docker-compose.prod.yml"' in SCRIPT
    assert 'docker compose' in SCRIPT


def test_topology_check_requires_exact_network_membership():
    assert 'saas_poster_backend' in SCRIPT
    assert 'saas_poster_egress_public' in SCRIPT
    assert 'saas_poster_ingress_public' in SCRIPT
    assert '[[ "$actual" == "$expected" ]]' in SCRIPT
    assert 'unexpected Docker network membership' in SCRIPT


def test_topology_check_covers_every_long_running_service():
    for service in (
        'db',
        'redis',
        'redis_broker',
        'egress_proxy',
        'django',
        'celery_worker',
        'celery_beat',
        'celery_worker_images',
        'frontend',
        'nginx',
    ):
        assert service in SCRIPT
    assert 'docker port "$nginx_id"' in SCRIPT
