import logging

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode

from apps.users.models import User


logger = logging.getLogger(__name__)

# Соль для подписи токена смены email
_SIGNING_SALT = 'email-change'
# Время жизни токена — 24 часа
_TOKEN_MAX_AGE = 86400


class ProfileService:
    """Сервис управления профилем пользователя: телефон, пароль, email."""

    @staticmethod
    def update_phone(user: User, phone: str) -> None:
        """Обновляет номер телефона пользователя."""
        user.phone = phone
        user.save(update_fields=['phone'])

    @staticmethod
    def change_password(user: User, current_password: str, new_password: str) -> None:
        """
        Меняет пароль пользователя.

        Проверяет текущий пароль перед сменой.
        """
        from apps.tenants.session_tokens import revoke_all_user_sessions

        with transaction.atomic():
            locked_user = User.objects.select_for_update().get(pk=user.pk)
            if not locked_user.check_password(current_password):
                raise ValueError('Неверный текущий пароль')
            ProfileService._validate_password(locked_user, new_password)
            locked_user.set_password(new_password)
            locked_user.save(update_fields=['password'])
            new_version = revoke_all_user_sessions(locked_user.pk)
        user.password = locked_user.password
        user.auth_version = new_version

    @staticmethod
    def request_email_change(user: User, new_email: str, current_password: str) -> None:
        """
        Инициирует смену email: формирует подписанный токен и шлёт письмо на новый адрес.

        Токен действителен 24 часа. Если новый email уже занят — бросает ValueError.
        """
        if not user.check_password(current_password):
            raise ValueError('Неверный текущий пароль')

        new_email = User.objects.normalize_email(new_email)
        if User.objects.filter(email__iexact=new_email).exists():
            raise ValueError('Этот email уже используется')

        token = signing.dumps(
            {
                'user_id': user.pk,
                'new_email': new_email,
                'current_email': user.email,
                'auth_version': user.auth_version,
            },
            salt=_SIGNING_SALT,
        )

        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000')
        # Fragment не отправляется HTTP-серверу и не попадает в access logs/Referer.
        # Frontend извлекает токен и подтверждает изменение отдельным POST.
        confirm_url = f'{frontend_url}/confirm-email#token={token}'

        send_mail(
            subject='Подтверждение смены email — MAP',
            message=(
                f'Для подтверждения нового email перейдите по ссылке:\n\n'
                f'{confirm_url}\n\n'
                f'Ссылка действительна 24 часа.'
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[new_email],
        )

    @staticmethod
    def confirm_email_change(token: str) -> User:
        """
        Подтверждает смену email по токену из письма.

        Возвращает обновлённого пользователя. Бросает ValueError при невалидном
        или истёкшем токене, либо если новый email уже занят другим пользователем.
        """
        try:
            data = signing.loads(token, salt=_SIGNING_SALT, max_age=_TOKEN_MAX_AGE)
        except signing.SignatureExpired:
            raise ValueError('Ссылка истекла. Запросите смену email заново.')
        except signing.BadSignature:
            raise ValueError('Недействительная ссылка.')

        from apps.tenants.session_tokens import revoke_all_user_sessions

        try:
            user_id = int(data['user_id'])
            token_auth_version = int(data['auth_version'])
            new_email = User.objects.normalize_email(data['new_email'])
            current_email = data['current_email']
        except (KeyError, TypeError, ValueError):
            raise ValueError('Недействительная ссылка.')

        try:
            with transaction.atomic():
                try:
                    user = User.objects.select_for_update().get(pk=user_id, is_active=True)
                except User.DoesNotExist:
                    raise ValueError('Недействительная ссылка.')
                if (
                    user.auth_version != token_auth_version
                    or user.email != current_email
                ):
                    raise ValueError('Ссылка уже использована или отозвана.')
                if User.objects.filter(email__iexact=new_email).exclude(pk=user.pk).exists():
                    raise ValueError('Этот email уже занят.')

                user.email = new_email
                user.save(update_fields=['email'])
                user.auth_version = revoke_all_user_sessions(user.pk)
        except IntegrityError as exc:
            raise ValueError('Этот email уже занят.') from exc
        return user

    @staticmethod
    def request_password_reset(email: str) -> None:
        """Одинаково ставит email-задачу для существующего и неизвестного адреса."""
        from apps.users.tasks import send_password_reset_email

        normalized = User.objects.normalize_email(email)
        user = User.objects.filter(email__iexact=normalized, is_active=True).first()
        try:
            send_password_reset_email.delay(user.pk if user is not None else None)
        except Exception:
            # Сбой публикации не раскрывает существование адреса через HTTP-ответ.
            logger.exception('Не удалось поставить письмо восстановления в очередь.')

    @staticmethod
    def confirm_password_reset(uid: str, token: str, new_password: str) -> User:
        """Одноразово меняет пароль и отзывает все существующие JWT-сессии."""
        from apps.tenants.session_tokens import revoke_all_user_sessions

        try:
            user_id = force_str(urlsafe_base64_decode(uid))
        except (TypeError, ValueError, OverflowError):
            raise ValueError('Недействительная или истёкшая ссылка.')

        with transaction.atomic():
            try:
                user = User.objects.select_for_update().get(pk=user_id, is_active=True)
            except (User.DoesNotExist, ValueError, TypeError):
                raise ValueError('Недействительная или истёкшая ссылка.')
            if not default_token_generator.check_token(user, token):
                raise ValueError('Недействительная или истёкшая ссылка.')
            ProfileService._validate_password(user, new_password)
            user.set_password(new_password)
            user.save(update_fields=['password'])
            user.auth_version = revoke_all_user_sessions(user.pk)
        return user

    @staticmethod
    def _validate_password(user: User, password: str) -> None:
        try:
            validate_password(password, user=user)
        except DjangoValidationError as exc:
            raise ValueError(' '.join(exc.messages)) from exc
