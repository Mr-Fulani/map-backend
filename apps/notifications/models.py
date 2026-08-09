import secrets

from django.db import models, transaction
from django.utils import timezone

from apps.tenants.models import Tenant

CONNECT_TOKEN_TTL_MINUTES = 15
CONNECT_TOKEN_CONSUMED = 'consumed'
CONNECT_TOKEN_EXPIRED = 'expired'
CONNECT_TOKEN_INVALID = 'invalid'


class TenantNotificationSettings(models.Model):
    """
    Настройки уведомлений для тенанта.

    Хранит Telegram chat_id и email для отправки алертов.
    Флаги notify_on_error / notify_on_critical контролируют уровни уведомлений.
    connect_token — одноразовый токен для привязки Telegram через бота.
    """

    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name='notification_settings',
    )
    telegram_chat_id = models.CharField(
        max_length=50, blank=True, default='',
        help_text='ID чата Telegram для отправки алертов',
    )
    telegram_username = models.CharField(
        max_length=100, blank=True, default='',
        help_text='Username подключённого Telegram-аккаунта (для отображения)',
    )
    notify_email = models.EmailField(
        blank=True, default='',
        help_text='Email для критических уведомлений и биллинга',
    )
    notify_on_error = models.BooleanField(
        default=True,
        help_text='Отправлять Telegram-уведомление при ошибках (level=error)',
    )
    notify_on_critical = models.BooleanField(
        default=True,
        help_text='Отправлять Telegram + Email при критических событиях (level=critical)',
    )
    # Одноразовый токен для привязки через /start в боте
    connect_token = models.CharField(max_length=64, blank=True, default='')
    connect_token_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = 'Настройки уведомлений'
        verbose_name_plural = 'Настройки уведомлений'

    def __str__(self):
        return f'Настройки уведомлений — {self.tenant.slug}'

    def generate_connect_token(self) -> str:
        """
        Генерирует новый одноразовый токен для привязки Telegram.

        TTL — 15 минут. Возвращает токен в plaintext.
        """
        token = secrets.token_urlsafe(32)
        self.connect_token = token
        self.connect_token_expires_at = (
            timezone.now() + timezone.timedelta(minutes=CONNECT_TOKEN_TTL_MINUTES)
        )
        self.save(update_fields=['connect_token', 'connect_token_expires_at'])
        return token

    def is_connect_token_valid(self, token: str) -> bool:
        """Проверяет токен на совпадение и срок действия."""
        if not self.connect_token or self.connect_token != token:
            return False
        if not self.connect_token_expires_at:
            return False
        return timezone.now() < self.connect_token_expires_at

    @classmethod
    def consume_connect_token(
        cls,
        token: str,
        *,
        chat_id: str,
        username: str,
    ) -> tuple['TenantNotificationSettings | None', str]:
        """Atomically bind exactly one chat and invalidate the one-time token."""
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(chat_id, str)
            or not chat_id
            or len(chat_id) > 50
            or not isinstance(username, str)
            or len(username) > 100
        ):
            return None, CONNECT_TOKEN_INVALID

        with transaction.atomic():
            matches = list(
                cls.objects.select_for_update()
                .select_related('tenant')
                .filter(connect_token=token)
                .order_by('pk')[:2],
            )
            # Duplicate live tokens are ambiguous and therefore fail closed.
            if len(matches) != 1:
                return None, CONNECT_TOKEN_INVALID
            settings_row = matches[0]
            if not settings_row.is_connect_token_valid(token):
                return None, CONNECT_TOKEN_EXPIRED
            settings_row.complete_telegram_connect(chat_id, username)
            return settings_row, CONNECT_TOKEN_CONSUMED

    def complete_telegram_connect(self, chat_id: str, username: str) -> None:
        """Сохраняет chat_id после успешной привязки и сбрасывает токен."""
        self.telegram_chat_id = chat_id
        self.telegram_username = username
        self.connect_token = ''
        self.connect_token_expires_at = None
        self.save(update_fields=[
            'telegram_chat_id', 'telegram_username',
            'connect_token', 'connect_token_expires_at',
        ])
