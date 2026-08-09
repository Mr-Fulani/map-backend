import pytest

from config.redis_config import parse_redis_location, validate_production_redis_layout


def test_parse_redis_location_requires_password_and_numeric_database():
    with pytest.raises(ValueError, match='пароль'):
        parse_redis_location('CACHE_REDIS_URL', 'redis://cache:6379/0')
    with pytest.raises(ValueError, match='числовой'):
        parse_redis_location('CACHE_REDIS_URL', 'redis://:secret@cache:6379/not-a-db')


def test_production_layout_separates_cache_process_and_durable_databases():
    validate_production_redis_layout(
        'redis://:cache-secret@redis:6379/0',
        'redis://:broker-secret@redis_broker:6379/0',
        'redis://:broker-secret@redis_broker:6379/1',
        'redis://:broker-secret@redis_broker:6379/2',
    )


def test_production_layout_rejects_cache_on_broker_process():
    with pytest.raises(ValueError, match='разные Redis-процессы'):
        validate_production_redis_layout(
            'redis://:secret@redis:6379/3',
            'redis://:secret@redis:6379/0',
            'redis://:secret@redis:6379/1',
            'redis://:secret@redis:6379/2',
        )


def test_production_layout_rejects_shared_durable_database():
    with pytest.raises(ValueError, match='разные Redis DB'):
        validate_production_redis_layout(
            'redis://:cache-secret@redis:6379/0',
            'redis://:broker-secret@redis_broker:6379/0',
            'redis://:broker-secret@redis_broker:6379/0',
            'redis://:broker-secret@redis_broker:6379/2',
        )
