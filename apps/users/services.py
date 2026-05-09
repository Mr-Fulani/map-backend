from django.conf import settings
from django.core import signing
from django.core.mail import send_mail

from apps.users.models import User

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
        if not user.check_password(current_password):
            raise ValueError('Неверный текущий пароль')
        user.set_password(new_password)
        user.save(update_fields=['password'])

    @staticmethod
    def request_email_change(user: User, new_email: str) -> None:
        """
        Инициирует смену email: формирует подписанный токен и шлёт письмо на новый адрес.

        Токен действителен 24 часа. Если новый email уже занят — бросает ValueError.
        """
        if User.objects.filter(email=new_email).exists():
            raise ValueError('Этот email уже используется')

        token = signing.dumps(
            {'user_id': user.pk, 'new_email': new_email},
            salt=_SIGNING_SALT,
        )

        frontend_url = getattr(settings, 'FRONTEND_URL', 'http://localhost:3000')
        confirm_url = f'{frontend_url}/confirm-email?token={token}'

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

        try:
            user = User.objects.get(pk=data['user_id'])
        except User.DoesNotExist:
            raise ValueError('Пользователь не найден.')

        new_email = data['new_email']
        if User.objects.filter(email=new_email).exclude(pk=user.pk).exists():
            raise ValueError('Этот email уже занят.')

        user.email = new_email
        user.save(update_fields=['email'])
        return user
