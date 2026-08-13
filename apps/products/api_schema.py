"""OpenAPI-only serializers for the products API.

The runtime views deliberately keep the platform-wide ``status/data/meta``
envelope.  These serializers describe that envelope for drf-spectacular
without coupling request handling to documentation-only validation.
"""

from django.conf import settings
from rest_framework import serializers

from apps.products.serializers import (
    ProductBulkActionJobSerializer,
    ProductCrossCodeSerializer,
    ProductDetailSerializer,
    ProductEnrichmentFactSerializer,
    ProductParseJobSerializer,
    ProductSerializer,
    TenantCatalogCategorySerializer,
    TenantCategoryMappingSerializer,
    VehicleFitmentSerializer,
)


class PaginationMetaSerializer(serializers.Serializer):
    total = serializers.IntegerField(min_value=0)
    page = serializers.IntegerField(min_value=1)
    page_size = serializers.IntegerField(min_value=1)
    next = serializers.URLField(allow_null=True)
    prev = serializers.URLField(allow_null=True)


class ProductListMetaSerializer(PaginationMetaSerializer):
    domain_counts = serializers.DictField(
        child=serializers.IntegerField(min_value=0),
    )


class ProductListResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductSerializer(many=True)  # type: ignore[assignment]
    meta = ProductListMetaSerializer()


class ProductResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductSerializer()  # type: ignore[assignment]


class ProductDetailResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductDetailSerializer()  # type: ignore[assignment]


class ProductBrandUpdateRequestSerializer(serializers.Serializer):
    brand = serializers.CharField(max_length=200, allow_blank=True)


class BrandOptionSerializer(serializers.Serializer):
    name = serializers.CharField()
    source = serializers.ChoiceField(  # type: ignore[assignment]
        choices=['category', 'avito', 'current'],
    )


class ProductBrandOptionsDataSerializer(serializers.Serializer):
    options = BrandOptionSerializer(many=True)
    catalog_loaded = serializers.BooleanField()
    catalog_synced_at = serializers.DateTimeField(allow_null=True)
    catalog_stale = serializers.BooleanField()
    category_scope = serializers.CharField(allow_null=True)


class ProductBrandOptionsResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductBrandOptionsDataSerializer()  # type: ignore[assignment]


class TaskDataSerializer(serializers.Serializer):
    task_id = serializers.CharField()


class TaskResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = TaskDataSerializer()  # type: ignore[assignment]


class CatalogCategoryListResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = TenantCatalogCategorySerializer(many=True)  # type: ignore[assignment]


class CatalogCategoryResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = TenantCatalogCategorySerializer()  # type: ignore[assignment]


class CatalogCategoryBranchToggleRequestSerializer(serializers.Serializer):
    is_active = serializers.BooleanField()


class CatalogCategoryBranchToggleDataSerializer(serializers.Serializer):
    is_active = serializers.BooleanField()
    affected_categories = serializers.IntegerField(min_value=0)
    affected_products = serializers.IntegerField(min_value=0)


class CatalogCategoryBranchToggleResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = CatalogCategoryBranchToggleDataSerializer()  # type: ignore[assignment]


class CatalogCategoryImageRequestSerializer(serializers.Serializer):
    image = serializers.ImageField(
        help_text='JPEG, PNG or WebP image, no larger than 5 MiB.',
    )


class CategoryMappingListResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = TenantCategoryMappingSerializer(many=True)  # type: ignore[assignment]


class CategoryMappingResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = TenantCategoryMappingSerializer()  # type: ignore[assignment]


class TenantSourceCategorySerializer(serializers.Serializer):
    source_category = serializers.CharField()
    catalog_category = serializers.IntegerField(allow_null=True)


class TenantSourceCategoryListResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = TenantSourceCategorySerializer(many=True)  # type: ignore[assignment]


class ProductCategoryAssignRequestSerializer(serializers.Serializer):
    product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        max_length=settings.API_BULK_MAX_ITEMS,
        allow_empty=False,
    )
    catalog_category = serializers.IntegerField(
        min_value=1,
        allow_null=True,
        required=False,
        help_text='Category ID or null to clear the manual assignment.',
    )


class ProductCategoryAssignDataSerializer(serializers.Serializer):
    updated_count = serializers.IntegerField(min_value=0)
    skipped_count = serializers.IntegerField(min_value=0)
    catalog_category = TenantCatalogCategorySerializer(allow_null=True)


class ProductCategoryAssignResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductCategoryAssignDataSerializer()  # type: ignore[assignment]


class ProductExcludeRequestSerializer(serializers.Serializer):
    product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        max_length=settings.API_BULK_MAX_ITEMS,
        allow_empty=False,
    )
    exclude = serializers.BooleanField(default=True, required=False)


class ProductBulkDeleteRequestSerializer(serializers.Serializer):
    product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        max_length=settings.API_BULK_MAX_ITEMS,
        allow_empty=False,
    )


class UpdatedCountDataSerializer(serializers.Serializer):
    updated_count = serializers.IntegerField(min_value=0)


class UpdatedCountResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = UpdatedCountDataSerializer()  # type: ignore[assignment]


class DeletedCountDataSerializer(serializers.Serializer):
    deleted_count = serializers.IntegerField(min_value=0)


class DeletedCountResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = DeletedCountDataSerializer()  # type: ignore[assignment]


class ProductParseRequestSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField(required=True)
    product_id = serializers.IntegerField(min_value=1, required=False)
    brand = serializers.CharField(required=False, allow_blank=True)
    article = serializers.CharField(required=False, allow_blank=True)
    source = serializers.CharField(  # type: ignore[assignment]
        required=False,
        allow_blank=True,
    )
    generate_after = serializers.BooleanField(default=False, required=False)


class ProductParseDataSerializer(serializers.Serializer):
    job_id = serializers.IntegerField(min_value=1)
    job_ids = serializers.ListField(child=serializers.IntegerField(min_value=1))
    state = serializers.CharField()
    generate_after = serializers.BooleanField()


class ProductParseResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductParseDataSerializer()  # type: ignore[assignment]


class ProductParseJobResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductParseJobSerializer()  # type: ignore[assignment]


class ProductBulkActionRequestSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField(required=True)
    action = serializers.CharField()
    product_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        max_length=settings.API_BULK_MAX_ITEMS,
        allow_empty=False,
    )
    source = serializers.CharField(required=False)  # type: ignore[assignment]
    batch_size = serializers.IntegerField(min_value=1, default=20, required=False)
    pause_seconds = serializers.IntegerField(
        min_value=0, max_value=3600, default=60, required=False,
    )


class ProductBulkActionResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductBulkActionJobSerializer()  # type: ignore[assignment]


class IdempotencyConflictResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='error')
    code = serializers.CharField(default='idempotency_conflict')
    message = serializers.CharField()


class ReviewProductSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    article = serializers.CharField()
    name = serializers.CharField()
    brand = serializers.CharField()
    category_1c = serializers.CharField()
    catalog_category_id = serializers.IntegerField(allow_null=True)


class ReviewQueueItemSerializer(serializers.Serializer):
    id = serializers.CharField()
    type = serializers.ChoiceField(choices=['fitment', 'fact', 'classification'])
    record_id = serializers.IntegerField()
    product = ReviewProductSerializer()
    title = serializers.CharField()
    reason = serializers.CharField()
    source_id = serializers.CharField()
    confidence = serializers.FloatField()
    needs_review = serializers.BooleanField()
    review_status = serializers.CharField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    payload = serializers.JSONField()


class ReviewQueueResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ReviewQueueItemSerializer(many=True)  # type: ignore[assignment]
    meta = PaginationMetaSerializer()


class ReviewQueueActionResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = serializers.JSONField(  # type: ignore[assignment]
        help_text='Updated review item, or product details for a classification review.',
    )


class FitmentListResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = VehicleFitmentSerializer(many=True)  # type: ignore[assignment]


class FitmentResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = VehicleFitmentSerializer()  # type: ignore[assignment]


class EnrichmentFactListResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductEnrichmentFactSerializer(many=True)  # type: ignore[assignment]


class EnrichmentFactResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductEnrichmentFactSerializer()  # type: ignore[assignment]


class CrossCodeListResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ProductCrossCodeSerializer(many=True)  # type: ignore[assignment]


class ListingIdsDataSerializer(serializers.Serializer):
    listing_ids = serializers.ListField(child=serializers.IntegerField(min_value=1))


class ProductPublishResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ListingIdsDataSerializer()  # type: ignore[assignment]


class ArchivedCountDataSerializer(serializers.Serializer):
    archived_count = serializers.IntegerField(min_value=0)


class ProductArchiveResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    data = ArchivedCountDataSerializer()  # type: ignore[assignment]


class ProductRegenerateRequestSerializer(serializers.Serializer):
    source = serializers.CharField(required=False)  # type: ignore[assignment]
    idempotency_key = serializers.UUIDField(required=True)  # type: ignore[assignment]


class ProductRegenerateErrorSerializer(serializers.Serializer):
    status = serializers.CharField(default='error')
    code = serializers.CharField()
    message = serializers.CharField(required=False)


class ProductRegenerateDataSerializer(serializers.Serializer):
    job_id = serializers.IntegerField(min_value=1, allow_null=True)
    state = serializers.CharField()
    generate_after = serializers.BooleanField()


class ProductRegenerateResponseSerializer(serializers.Serializer):
    status = serializers.CharField(default='ok')
    message = serializers.CharField()
    data = ProductRegenerateDataSerializer()  # type: ignore[assignment]
