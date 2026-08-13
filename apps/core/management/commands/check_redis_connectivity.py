import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


REDIS_URL_SETTINGS = (
    'CACHE_REDIS_URL',
    'CELERY_BROKER_URL',
    'CELERY_RESULT_BACKEND',
    'COORDINATION_REDIS_URL',
)

EXPECTED_MAXMEMORY_BYTES = {
    'CACHE_REDIS_URL': 160 * 1024 * 1024,
    'CELERY_BROKER_URL': 224 * 1024 * 1024,
    'CELERY_RESULT_BACKEND': 224 * 1024 * 1024,
    'COORDINATION_REDIS_URL': 224 * 1024 * 1024,
}
MIN_WRITE_HEADROOM_BYTES = 16 * 1024 * 1024


def _redis_client(url):
    # Import lazily so static contract checks do not need the application
    # dependency set. The production image installs the hash-locked redis client.
    from redis import Redis

    return Redis.from_url(
        url,
        socket_connect_timeout=5,
        socket_timeout=5,
    )


class Command(BaseCommand):
    requires_system_checks = []
    help = (
        'Проверяет credentials, projected memory headroom и запись/удаление '
        'короткоживущего namespaced key для всех production Redis URL.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--require-target-limits',
            action='store_true',
            help='Дополнительно требует точного target maxmemory после recreate.',
        )

    def handle(self, *args, **options):
        require_target_limits = bool(options.get('require_target_limits'))
        for setting_name in REDIS_URL_SETTINGS:
            client = None
            probe_key = f'saas-poster:deploy-preflight:{uuid.uuid4()}'
            try:
                client = _redis_client(getattr(settings, setting_name))
                if client.ping() is not True:
                    raise RuntimeError('Redis PING returned an unexpected response')
                memory = client.info(section='memory')
                used_memory = int(memory['used_memory'])
                actual_maxmemory = int(memory['maxmemory'])
                expected_maxmemory = EXPECTED_MAXMEMORY_BYTES[setting_name]
                if used_memory + MIN_WRITE_HEADROOM_BYTES > expected_maxmemory:
                    raise RuntimeError('Redis projected target capacity has no headroom')
                if require_target_limits and actual_maxmemory != expected_maxmemory:
                    raise RuntimeError('Redis maxmemory does not match target contract')
                if client.set(probe_key, b'1', ex=10, nx=True) is not True:
                    raise RuntimeError('Redis write probe was rejected')
                if client.delete(probe_key) != 1:
                    raise RuntimeError('Redis write probe cleanup failed')
            except Exception as exc:
                # Never include the URL or the provider exception text: both may
                # contain credentials supplied through the production environment.
                raise CommandError(
                    f'Redis connectivity check failed for {setting_name} '
                    f'({type(exc).__name__}).'
                ) from None
            finally:
                if client is not None:
                    try:
                        # If the strict cleanup check failed, TTL still bounds
                        # this namespaced key; best-effort delete shortens it.
                        client.delete(probe_key)
                    except Exception:
                        pass
                    try:
                        client.close()
                    except Exception:
                        # The process is one-shot; a close failure must not hide the
                        # result of the bounded PING performed above.
                        pass

            self.stdout.write(self.style.SUCCESS(f'{setting_name}: ok'))
