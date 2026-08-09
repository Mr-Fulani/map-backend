from django.db import models

from apps.core.models import SoftDeleteModel
from apps.tenants.models import Tenant


class DataSourceConnection(SoftDeleteModel):
    """Подключение к источнику данных для импорта товаров (1С, CSV и т.д.)."""

    TYPE_1C_HTTP = '1c_http'
    TYPE_1C_XML = '1c_xml'
    TYPE_CSV = 'csv'
    TYPE_CHOICES = [
        (TYPE_1C_HTTP, '1С HTTP'),
        (TYPE_1C_XML, '1С XML'),
        (TYPE_CSV, 'CSV/Excel'),
    ]

    STATUS_NEVER = 'never'
    STATUS_OK = 'ok'
    STATUS_ERROR = 'error'

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE,
        related_name='datasource_connections', verbose_name='Тенант',
    )
    name = models.CharField(max_length=200, verbose_name='Название')
    type = models.CharField(max_length=20, choices=TYPE_CHOICES, verbose_name='Тип источника')
    is_active = models.BooleanField(default=True, verbose_name='Активен')
    credentials = models.BinaryField(verbose_name='Учётные данные (зашифровано)')
    last_sync_at = models.DateTimeField(null=True, blank=True, verbose_name='Последняя синхронизация')
    last_sync_status = models.CharField(
        max_length=20, default=STATUS_NEVER, verbose_name='Статус последней синхронизации',
    )
    last_error = models.TextField(blank=True, verbose_name='Последняя ошибка')
    content_hash = models.CharField(
        max_length=64, blank=True, db_index=True,
        verbose_name='SHA-256 загруженного файла',
        help_text='Для CSV/Excel — хэш содержимого, чтобы ловить повторную загрузку того же файла.',
    )

    class Meta:
        verbose_name = 'Источник данных'
        verbose_name_plural = 'Источники данных'
        ordering = ['name']

    def __str__(self):
        return f'{self.tenant.slug} / {self.name} ({self.type})'

    def soft_delete(self):
        self.is_active = False
        self.save(update_fields=['is_active', 'updated_at'])
        super().soft_delete()
