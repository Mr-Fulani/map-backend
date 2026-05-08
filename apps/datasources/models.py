from django.db import models

from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


class DataSourceConnection(TimestampedModel):
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

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='datasource_connections')
    name = models.CharField(max_length=200)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    is_active = models.BooleanField(default=True)
    credentials = models.BinaryField()
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_status = models.CharField(max_length=20, default=STATUS_NEVER)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.tenant.slug} / {self.name} ({self.type})'
