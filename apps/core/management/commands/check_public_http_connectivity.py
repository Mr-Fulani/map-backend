from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.url_security import REDIRECT_NONE, request_public_http_url


# A fixed synthetic payment ID keeps the GET side-effect-free while exercising
# DNS admission, the production proxy, TLS and YooKassa credentials.
YOOKASSA_PREFLIGHT_URL = (
    'https://api.yookassa.ru/v3/payments/'
    '00000000-000f-5000-8000-000000000000'
)
YOOKASSA_NOT_FOUND_STATUS = 404


def _request_preflight(
    url: str,
    auth: tuple[str, str] | None = None,
):
    timeout = (
        settings.YOOKASSA_API_CONNECT_TIMEOUT_SECONDS,
        settings.YOOKASSA_API_READ_TIMEOUT_SECONDS,
    )
    if auth is None:
        return request_public_http_url(
            url,
            method='GET',
            timeout=timeout,
            status_only=True,
            redirect_policy=REDIRECT_NONE,
            max_redirects=0,
            max_elapsed_seconds=settings.YOOKASSA_API_MAX_ELAPSED_SECONDS,
        )
    return request_public_http_url(
        url,
        method='GET',
        timeout=timeout,
        auth=auth,
        status_only=True,
        redirect_policy=REDIRECT_NONE,
        max_redirects=0,
        max_elapsed_seconds=settings.YOOKASSA_API_MAX_ELAPSED_SECONDS,
    )


class Command(BaseCommand):
    requires_system_checks = []
    help = (
        'Проверяет production public HTTPS transport и YooKassa credentials '
        'без создания или изменения платежа.'
    )

    def handle(self, *args, **options):
        auth = None
        if settings.BILLING_ENABLED:
            preflight_url = YOOKASSA_PREFLIGHT_URL
            auth = (
                settings.YOOKASSA_SHOP_ID,
                settings.YOOKASSA_SECRET_KEY,
            )
            expected_status = YOOKASSA_NOT_FOUND_STATUS
            success_message = 'Public HTTPS transport and YooKassa credentials: ok'
        else:
            preflight_url = settings.PUBLIC_HTTP_PREFLIGHT_URL
            expected_status = 200
            success_message = 'Public HTTPS transport: ok (billing disabled)'
        try:
            response = _request_preflight(preflight_url, auth)
            if response.status_code != expected_status:
                raise RuntimeError('Public HTTPS preflight returned an unexpected status')
        except Exception as exc:
            # Provider/transport errors can include endpoint or credential data.
            raise CommandError(
                f'Public HTTPS connectivity check failed ({type(exc).__name__}).',
            ) from None

        self.stdout.write(self.style.SUCCESS(success_message))
