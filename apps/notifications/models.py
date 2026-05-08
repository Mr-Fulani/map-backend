from django.db import models

from apps.tenants.models import Tenant


class TenantNotificationSettings(models.Model):
    """
    Настройки уведомлений для тенанта.

    Хранит Telegram chat_id и email для отправки алертов.
    Флаги notify_on_error / notify_on_critical контролируют уровни уведомлений.
    """

    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name='notification_settings',
    )
    telegram_chat_id = models.CharField(
        max_length=50, blank=True, default='',
        help_text='ID чата Telegram для отправки алертов',
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

    class Meta:
        verbose_name = 'Настройки уведомлений'
        verbose_name_plural = 'Настройки уведомлений'

    def __str__(self):
        return f'Настройки уведомлений — {self.tenant.slug}'
