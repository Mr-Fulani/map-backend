from dataclasses import dataclass
from urllib.parse import unquote, urlparse


@dataclass(frozen=True)
class RedisLocation:
    endpoint: tuple[str, int]
    database: int
    password: str
    scheme: str


def parse_redis_location(name: str, value: str) -> RedisLocation:
    parsed = urlparse(value)
    if parsed.scheme not in {'redis', 'rediss'}:
        raise ValueError(f'{name} должен использовать redis:// или rediss://.')
    if not parsed.hostname:
        raise ValueError(f'{name} должен содержать hostname.')
    if not parsed.password:
        raise ValueError(f'{name} должен содержать пароль.')

    database_text = parsed.path.lstrip('/') or '0'
    try:
        database = int(database_text)
    except ValueError as exc:
        raise ValueError(f'{name} должен содержать числовой Redis DB.') from exc
    if database < 0:
        raise ValueError(f'{name} Redis DB не может быть отрицательным.')

    return RedisLocation(
        endpoint=(parsed.hostname, parsed.port or 6379),
        database=database,
        password=unquote(parsed.password),
        scheme=parsed.scheme,
    )


def validate_production_redis_layout(
    cache_url: str,
    broker_url: str,
    result_url: str,
    coordination_url: str,
    *,
    cache_server_password: str,
    broker_server_password: str,
) -> None:
    cache = parse_redis_location('CACHE_REDIS_URL', cache_url)
    broker = parse_redis_location('CELERY_BROKER_URL', broker_url)
    result = parse_redis_location('CELERY_RESULT_BACKEND', result_url)
    coordination = parse_redis_location('COORDINATION_REDIS_URL', coordination_url)

    if cache.password != cache_server_password:
        raise ValueError(
            'CACHE_REDIS_URL должен использовать CACHE_REDIS_PASSWORD '
            'запущенного cache Redis.'
        )
    if broker.password != broker_server_password:
        raise ValueError(
            'CELERY_BROKER_URL должен использовать CELERY_REDIS_PASSWORD '
            'запущенного durable Redis.'
        )
    if cache_server_password == broker_server_password:
        raise ValueError(
            'Cache Redis и durable Redis должны использовать разные пароли.'
        )

    if cache.endpoint == broker.endpoint:
        raise ValueError(
            'Cache и Celery broker должны использовать разные Redis-процессы.'
        )
    if result.endpoint != broker.endpoint or coordination.endpoint != broker.endpoint:
        raise ValueError(
            'Result backend и coordination store должны использовать durable broker Redis.'
        )
    if (
        result.password != broker.password
        or coordination.password != broker.password
        or result.scheme != broker.scheme
        or coordination.scheme != broker.scheme
    ):
        raise ValueError(
            'Broker, result backend и coordination store должны использовать '
            'одинаковые credentials и scheme.'
        )

    durable_databases = {broker.database, result.database, coordination.database}
    if len(durable_databases) != 3:
        raise ValueError(
            'Broker, result backend и coordination store должны использовать разные Redis DB.'
        )
