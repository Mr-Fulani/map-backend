from django.db import models

from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


class AvitoCategory(models.Model):
    """Дерево категорий Avito (read-only, обновляется из официального JSON)."""
    avito_id = models.IntegerField(unique=True)
    name = models.CharField(max_length=200)
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True,
                               related_name='children')
    is_leaf = models.BooleanField(default=False)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f'{self.name} (id={self.avito_id})'


class CategoryMapping(TimestampedModel):
    MARKETPLACE_AVITO = 'avito'

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='category_mappings')
    marketplace = models.CharField(max_length=50, default=MARKETPLACE_AVITO)
    category_source = models.CharField(max_length=300)
    category_target = models.CharField(max_length=200)
    category_id = models.IntegerField()
    attributes_map = models.JSONField(default=dict)
    version = models.PositiveSmallIntegerField(default=1)

    class Meta:
        unique_together = [('tenant', 'marketplace', 'category_source')]

    def __str__(self):
        return f'{self.tenant.slug}: {self.category_source} → {self.category_target}'
