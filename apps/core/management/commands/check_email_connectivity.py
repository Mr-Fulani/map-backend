from django.core.mail import get_connection
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    requires_system_checks = []
    help = (
        'Проверяет HTTP CONNECT, SMTP greeting, STARTTLS и credentials '
        'production email backend без отправки письма.'
    )

    def handle(self, *args, **options):
        connection = None
        try:
            connection = get_connection(fail_silently=False)
            if connection.open() is not True:
                raise RuntimeError('Email backend did not open a new connection')
        except Exception as exc:
            # Provider errors can contain endpoint or authentication details.
            # The preflight only exposes the exception class to deployment logs.
            raise CommandError(
                f'Email connectivity check failed ({type(exc).__name__}).',
            ) from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    # This is a one-shot process; close failure must not mask the
                    # bounded handshake/login result above.
                    pass

        self.stdout.write(self.style.SUCCESS('SMTP connectivity and credentials: ok'))
