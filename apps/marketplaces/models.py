import uuid

from django.db import models

from apps.core.models import TimestampedModel
from apps.tenants.models import Tenant


class AvitoCategory(models.Model):
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


class MarketplaceAccount(TimestampedModel):
    MARKETPLACE_AVITO = 'avito'
    MARKETPLACE_CHOICES = [(MARKETPLACE_AVITO, 'Avito')]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE,
                               related_name='marketplace_accounts')
    marketplace = models.CharField(max_length=50, choices=MARKETPLACE_CHOICES,
                                   default=MARKETPLACE_AVITO)
    name = models.CharField(max_length=200)
    external_id = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    credentials_enc = models.BinaryField()
    token_expires_at = models.DateTimeField(null=True, blank=True)
    requests_this_hour = models.PositiveIntegerField(default=0)
    hour_bucket_reset_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('tenant', 'marketplace', 'external_id')]

    def __str__(self):
        return f'{self.tenant.slug} / {self.name}'


class Listing(TimestampedModel):
    STATUS_DRAFT = 'draft'
    STATUS_PENDING = 'pending'
    STATUS_ACTIVE = 'active'
    STATUS_REJECTED = 'rejected'
    STATUS_ARCHIVED = 'archived'
    STATUS_DELETED = 'deleted'
    STATUS_REQUIRES_REVIEW = 'requires_review'
    STATUS_LIMIT_REACHED = 'limit_reached'
    STATUS_CHOICES = [
        (STATUS_DRAFT, 'Черновик'),
        (STATUS_PENDING, 'На модерации'),
        (STATUS_ACTIVE, 'Активно'),
        (STATUS_REJECTED, 'Отклонено'),
        (STATUS_ARCHIVED, 'В архиве'),
        (STATUS_DELETED, 'Удалено'),
        (STATUS_REQUIRES_REVIEW, 'Требует проверки'),
        (STATUS_LIMIT_REACHED, 'Лимит достигнут'),
    ]

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name='listings')
    product = models.ForeignKey('products.Product', on_delete=models.CASCADE,
                                related_name='listings')
    account = models.ForeignKey(MarketplaceAccount, on_delete=models.CASCADE,
                                related_name='listings')

    external_id = models.CharField(max_length=100, null=True, blank=True, unique=True)
    external_url = models.URLField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    rejection_reason = models.TextField(blank=True)

    title = models.CharField(max_length=300, blank=True)
    description_ai = models.TextField(blank=True)
    ai_confidence = models.FloatField(null=True, blank=True)
    price_on_listing = models.DecimalField(max_digits=12, decimal_places=2)

    publish_idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True)
    retry_count = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('tenant', 'product', 'account')]
        indexes = [
            models.Index(fields=['tenant', 'status']),
            models.Index(fields=['account', 'status']),
            models.Index(fields=['next_retry_at']),
        ]

    def __str__(self):
        return f'Listing #{self.pk} [{self.status}]'


class ListingStats(models.Model):
    """Ежедневный снимок статистики листинга с Avito."""

    listing = models.ForeignKey(
        Listing, on_delete=models.CASCADE, related_name='stats',
    )
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name='listing_stats',
    )
    date = models.DateField()
    views = models.PositiveIntegerField(default=0)
    contacts = models.PositiveIntegerField(default=0)
    impressions = models.PositiveIntegerField(default=0)
    ctr = models.FloatField(default=0.0)

    class Meta:
        unique_together = [('listing', 'date')]
        indexes = [
            models.Index(fields=['tenant', 'date']),
        ]

    def __str__(self):
        return f'Stats listing#{self.listing_id} {self.date}'
