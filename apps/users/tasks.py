"""Asynchronous security emails whose public endpoints must return uniformly."""

from urllib.parse import urlencode

from celery import shared_task
from django.conf import settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.notifications.email import EmailNotifier
from apps.users.models import User
from apps.users.tokens import (
    current_password_reset_timestamp,
    make_password_reset_token_at,
    password_reset_datetime,
)


@shared_task(
    bind=True,
    queue='notifications',
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={'max_retries': 5},
    acks_late=True,
    reject_on_worker_lost=True,
)
def send_password_reset_email(
    self,
    user_id: int | None,
    token_timestamp: int | None = None,
):
    """Generate the one-time token in the worker and send it when user still exists."""
    if user_id is None:
        return {'sent': False}
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None or not user.has_usable_password():
        return {'sent': False}

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    stable_timestamp = (
        int(token_timestamp)
        if token_timestamp is not None
        else current_password_reset_timestamp()
    )
    token = make_password_reset_token_at(user, stable_timestamp)
    fragment = urlencode({'uid': uid, 'token': token})
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
    # URL fragments никогда не передаются серверу и поэтому не попадают в
    # HTTP access logs или Referer при загрузке страницы восстановления.
    reset_url = f'{frontend_url}/reset-password#{fragment}'
    sent = EmailNotifier().send(
        user.email,
        'Восстановление пароля — MAP',
        (
            'Для установки нового пароля перейдите по ссылке:\n\n'
            f'{reset_url}\n\n'
            'Если вы не запрашивали восстановление, проигнорируйте письмо.'
        ),
        idempotency_key=f'map-password-reset/{user.pk}-{stable_timestamp}',
        message_date=password_reset_datetime(stable_timestamp),
    )
    if not sent:
        raise RuntimeError('Password reset email delivery failed.')
    return {'sent': True}
