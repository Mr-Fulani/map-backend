import pytest

from config.redis_config import parse_redis_location, validate_production_redis_layout


def validate_layout(
    cache_url='redis://:cache-secret@redis:6379/0',
    broker_url='redis://:broker-secret@redis_broker:6379/0',
    result_url='redis://:broker-secret@redis_broker:6379/1',
    coordination_url='redis://:broker-secret@redis_broker:6379/2',
    *,
    cache_password='cache-secret',
    broker_password='broker-secret',
):
    return validate_production_redis_layout(
        cache_url,
        broker_url,
        result_url,
        coordination_url,
        cache_server_password=cache_password,
        broker_server_password=broker_password,
    )


def test_parse_redis_location_requires_password_and_numeric_database():
    with pytest.raises(ValueError, match='пароль'):
        parse_redis_location('CACHE_REDIS_URL', 'redis://cache:6379/0')
    with pytest.raises(ValueError, match='числовой'):
        parse_redis_location('CACHE_REDIS_URL', 'redis://:secret@cache:6379/not-a-db')


def test_production_layout_separates_cache_process_and_durable_databases():
    validate_layout()


def test_production_layout_decodes_passwords_before_server_comparison():
    validate_layout(
        cache_url='redis://:cache%40secret@redis:6379/0',
        cache_password='cache@secret',
    )


def test_production_layout_rejects_cache_on_broker_process():
    with pytest.raises(ValueError, match='разные Redis-процессы'):
        validate_layout(
            cache_url='redis://:cache-secret@redis:6379/3',
            broker_url='redis://:broker-secret@redis:6379/0',
            result_url='redis://:broker-secret@redis:6379/1',
            coordination_url='redis://:broker-secret@redis:6379/2',
        )


def test_production_layout_rejects_shared_durable_database():
    with pytest.raises(ValueError, match='разные Redis DB'):
        validate_layout(
            result_url='redis://:broker-secret@redis_broker:6379/0',
        )


@pytest.mark.parametrize(
    ('overrides', 'message'),
    [
        ({'cache_password': 'wrong-cache'}, 'CACHE_REDIS_PASSWORD'),
        ({'broker_password': 'wrong-broker'}, 'CELERY_REDIS_PASSWORD'),
        (
            {
                'cache_url': 'redis://:shared@redis:6379/0',
                'broker_url': 'redis://:shared@redis_broker:6379/0',
                'result_url': 'redis://:shared@redis_broker:6379/1',
                'coordination_url': 'redis://:shared@redis_broker:6379/2',
                'cache_password': 'shared',
                'broker_password': 'shared',
            },
            'разные пароли',
        ),
    ],
)
def test_production_layout_rejects_runtime_password_mismatch(overrides, message):
    with pytest.raises(ValueError, match=message):
        validate_layout(**overrides)
