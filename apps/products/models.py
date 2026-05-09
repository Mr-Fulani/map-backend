from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.core.models import TimestampedModel
from apps.datasources.models import DataSourceConnection
from apps.tenants.models import Tenant


class Product(TimestampedModel):
    CONDITION_NEW = 'new'
    CONDITION_USED = 'used'
    CONDITION_CHOICES = [(CONDITION_NEW, 'Новый'), (CONDITION_USED, 'Б/у')]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='products')
    datasource = models.ForeignKey(DataSourceConnection, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='products')

    uuid_1c = models.UUIDField(null=True, blank=True)
    article = models.CharField(max_length=100)
    cross_numbers = ArrayField(models.CharField(max_length=50), default=list, blank=True)
    oem_numbers = ArrayField(models.CharField(max_length=50), default=list, blank=True)

    name = models.CharField(max_length=500)
    brand = models.CharField(max_length=200, blank=True)
    category_1c = models.CharField(max_length=300, blank=True)
    condition = models.CharField(max_length=10, choices=CONDITION_CHOICES, default=CONDITION_NEW)
    applicability = models.JSONField(default=list)
    description_1c = models.TextField(blank=True)

    price = models.DecimalField(max_digits=12, decimal_places=2)
    stock_qty = models.PositiveIntegerField(default=0)
    warehouse = models.CharField(max_length=200, blank=True)

    export_enabled = models.BooleanField(default=False)
    sync_at = models.DateTimeField(null=True, blank=True)
    hash_1c = models.CharField(max_length=64, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['tenant', 'article']),
            models.Index(fields=['tenant', 'export_enabled', 'sync_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'uuid_1c'],
                condition=models.Q(uuid_1c__isnull=False),
                name='unique_tenant_uuid_1c',
            ),
            models.UniqueConstraint(
                fields=['tenant', 'datasource', 'article'],
                name='unique_tenant_datasource_article',
            ),
        ]

    def __str__(self):
        return f'{self.article} — {self.name}'


class ProductImage(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    s3_key = models.CharField(max_length=500)
    s3_key_thumb = models.CharField(max_length=500, blank=True)
    url_source = models.URLField(blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['position']
        unique_together = [('product', 'sha256')]
