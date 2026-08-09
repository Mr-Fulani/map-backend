"""Asynchronous security emails whose public endpoints must return uniformly."""

from urllib.parse import urlencode

from celery import shared_task
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from apps.users.models import User


@shared_task(
    bind=True,
    queue='notifications',
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={'max_retries': 5},
)
def send_password_reset_email(self, user_id: int | None):
    """Generate the one-time token in the worker and send it when user still exists."""
    if user_id is None:
        return {'sent': False}
    user = User.objects.filter(pk=user_id, is_active=True).first()
    if user is None or not user.has_usable_password():
        return {'sent': False}

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    fragment = urlencode({'uid': uid, 'token': token})
    frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000').rstrip('/')
    # URL fragments никогда не передаются серверу и поэтому не попадают в
    # HTTP access logs или Referer при загрузке страницы восстановления.
    reset_url = f'{frontend_url}/reset-password#{fragment}'
    send_mail(
        subject='Восстановление пароля — MAP',
        message=(
            'Для установки нового пароля перейдите по ссылке:\n\n'
            f'{reset_url}\n\n'
            'Если вы не запрашивали восстановление, проигнорируйте письмо.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user.email],
    )
    return {'sent': True}
