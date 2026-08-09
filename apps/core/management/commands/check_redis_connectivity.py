from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


REDIS_URL_SETTINGS = (
    'CACHE_REDIS_URL',
    'CELERY_BROKER_URL',
    'CELERY_RESULT_BACKEND',
    'COORDINATION_REDIS_URL',
)


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
        'Проверяет сетевую доступность и credentials всех production Redis URL '
        'без записи ключей или постановки задач.'
    )

    def handle(self, *args, **options):
        for setting_name in REDIS_URL_SETTINGS:
            client = None
            try:
                client = _redis_client(getattr(settings, setting_name))
                if client.ping() is not True:
                    raise RuntimeError('Redis PING returned an unexpected response')
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
                        client.close()
                    except Exception:
                        # The process is one-shot; a close failure must not hide the
                        # result of the bounded PING performed above.
                        pass

            self.stdout.write(self.style.SUCCESS(f'{setting_name}: ok'))
