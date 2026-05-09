from django.db import models

from apps.tenants.models import Tenant


class SyncLog(models.Model):
    EVENT_DATASOURCE_IMPORT = 'datasource_import'
    EVENT_DESCRIPTION_GEN = 'description_gen'
    EVENT_LISTING_PUBLISH = 'listing_publish'
    EVENT_LISTING_UPDATE = 'listing_update'
    EVENT_LISTING_PRICE_UPDATE = 'listing_price_update'
    EVENT_LISTING_UNPUBLISH = 'listing_unpublish'
    EVENT_LISTING_DELETE = 'listing_delete'
    EVENT_LISTING_ERROR = 'listing_error'
    EVENT_MODERATION = 'moderation'
    EVENT_BILLING = 'billing_event'
    EVENT_ANTI_BAN = 'anti_ban_trigger'
    EVENT_RATE_LIMIT = 'rate_limit_hit'

    EVENT_CHOICES = [
        (EVENT_DATASOURCE_IMPORT, 'Импорт данных'),
        (EVENT_DESCRIPTION_GEN, 'Генерация описания'),
        (EVENT_LISTING_PUBLISH, 'Публикация'),
        (EVENT_LISTING_UPDATE, 'Обновление'),
        (EVENT_LISTING_PRICE_UPDATE, 'Обновление цены'),
        (EVENT_LISTING_UNPUBLISH, 'Снятие с публикации'),
        (EVENT_LISTING_DELETE, 'Удаление'),
        (EVENT_LISTING_ERROR, 'Ошибка листинга'),
        (EVENT_MODERATION, 'Модерация'),
        (EVENT_BILLING, 'Биллинг'),
        (EVENT_ANTI_BAN, 'Анти-бан'),
        (EVENT_RATE_LIMIT, 'Rate limit'),
    ]

    STATUS_OK = 'ok'
    STATUS_WARN = 'warn'
    STATUS_ERROR = 'error'
    STATUS_CHOICES = [(STATUS_OK, 'OK'), (STATUS_WARN, 'Предупреждение'), (STATUS_ERROR, 'Ошибка')]

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='sync_logs', verbose_name='Тенант',
    )
    product = models.ForeignKey(
        'products.Product', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='sync_logs', verbose_name='Товар',
    )
    listing = models.ForeignKey(
        'marketplaces.Listing', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='sync_logs', verbose_name='Листинг',
    )
    event_type = models.CharField(max_length=30, choices=EVENT_CHOICES, verbose_name='Тип события')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, verbose_name='Статус')
    message = models.TextField(verbose_name='Сообщение')
    payload = models.JSONField(default=dict, verbose_name='Payload (JSON)')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='Время события')

    class Meta:
        verbose_name = 'Лог синхронизации'
        verbose_name_plural = 'Логи синхронизации'
        indexes = [
            models.Index(fields=['tenant', '-created_at']),
            models.Index(fields=['tenant', 'event_type', 'status']),
        ]

    def __str__(self):
        return f'[{self.event_type}] {self.status} — {self.tenant.slug}'
