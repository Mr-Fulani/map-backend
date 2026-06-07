from django.core.files.storage import default_storage
from rest_framework import serializers

from apps.products.models import (
    Product, ProductAttribute, ProductBulkActionJob, ProductCatalogClassification,
    ProductCrossCode, ProductEnrichmentFact, ProductImage, ProductParseJob, TenantCatalogCategory,
    TenantCategoryMapping, VehicleFitment,
)


class ProductImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductImage
        fields = ['id', 's3_key', 's3_key_thumb', 'url_source', 'position', 'uploaded_at']


class ProductCatalogClassificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCatalogClassification
        fields = [
            'domain', 'confidence', 'source', 'reason', 'needs_review',
            'review_status', 'reviewed_at', 'updated_at',
        ]


class TenantCatalogCategorySerializer(serializers.ModelSerializer):
    default_image_url = serializers.SerializerMethodField()
    root_domain_slug = serializers.CharField(source='root_domain.slug', read_only=True)
    root_domain_name = serializers.CharField(source='root_domain.name', read_only=True)

    class Meta:
        model = TenantCatalogCategory
        fields = [
            'id', 'name', 'normalized_name', 'parent', 'root_domain',
            'root_domain_slug', 'root_domain_name', 'domain', 'aliases', 'external_source',
            'external_id', 'default_image_s3_key', 'default_image_source_name',
            'default_image_url', 'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'normalized_name', 'default_image_s3_key',
            'default_image_source_name', 'default_image_url',
            'root_domain_slug', 'root_domain_name', 'created_at', 'updated_at',
        ]

    def get_default_image_url(self, obj) -> str:
        if not obj.default_image_s3_key:
            return ''
        url = default_storage.url(obj.default_image_s3_key)
        if url.startswith('/'):
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(url)
        return url


class TenantCategoryMappingSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    category_domain = serializers.CharField(source='category.domain', read_only=True)

    class Meta:
        model = TenantCategoryMapping
        fields = [
            'id', 'source_category', 'category', 'category_name',
            'category_domain', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'category_name', 'category_domain', 'created_at', 'updated_at']


class ProductSerializer(serializers.ModelSerializer):
    images = ProductImageSerializer(many=True, read_only=True)
    images_count = serializers.SerializerMethodField()
    primary_thumb_url = serializers.SerializerMethodField()
    ai_status = serializers.SerializerMethodField()
    enrichment_status = serializers.SerializerMethodField()
    enrichment_summary = serializers.SerializerMethodField()
    catalog_classification = ProductCatalogClassificationSerializer(read_only=True)
    catalog_category = TenantCatalogCategorySerializer(read_only=True)
    brand_ref_name = serializers.CharField(source='brand_ref.name', read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'uuid_1c', 'article', 'name', 'brand', 'brand_ref',
            'brand_ref_name', 'category_1c', 'catalog_category',
            'condition', 'price', 'stock_qty', 'warehouse',
            'export_enabled', 'sync_at', 'images', 'images_count', 'primary_thumb_url',
            'title_ai', 'description_ai', 'ai_status', 'enrichment_status',
            'enrichment_summary', 'catalog_classification',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['uuid_1c', 'brand_ref', 'brand_ref_name', 'sync_at', 'created_at', 'updated_at']

    def get_images_count(self, obj) -> int:
        """Возвращает количество изображений товара."""
        return len(obj.images.all())

    def get_primary_thumb_url(self, obj) -> str:
        """Возвращает URL миниатюры главного (или первого) изображения."""
        images = list(obj.images.all())
        img = next((i for i in images if i.is_primary), None) or (images[0] if images else None)
        if not img or not img.s3_key_thumb:
            category = getattr(obj, 'catalog_category', None)
            if category and category.default_image_s3_key:
                url = default_storage.url(category.default_image_s3_key)
                if url.startswith('/'):
                    request = self.context.get('request')
                    if request:
                        return request.build_absolute_uri(url)
                return url
            return ''
        url = default_storage.url(img.s3_key_thumb)
        if url.startswith('/'):
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(url)
        return url

    def get_ai_status(self, obj) -> str:
        return 'ready' if obj.title_ai and obj.description_ai else 'missing'

    def get_enrichment_status(self, obj) -> str:
        has_enrichment = (
            getattr(obj, 'attributes_count', 0)
            or getattr(obj, 'cross_codes_count', 0)
            or getattr(obj, 'fitments_count', 0)
        )
        if has_enrichment:
            return 'ready'
        latest_jobs = list(getattr(obj, '_prefetched_objects_cache', {}).get('parse_jobs', []))
        if latest_jobs:
            return latest_jobs[0].status
        return 'missing'

    def get_enrichment_summary(self, obj) -> dict:
        latest_jobs = list(getattr(obj, '_prefetched_objects_cache', {}).get('parse_jobs', []))
        latest_job = latest_jobs[0] if latest_jobs else None
        return {
            'attributes_count': getattr(obj, 'attributes_count', 0),
            'cross_codes_count': getattr(obj, 'cross_codes_count', 0),
            'fitments_count': getattr(obj, 'fitments_count', 0),
            'latest_parse_status': latest_job.status if latest_job else '',
            'latest_parse_at': latest_job.created_at if latest_job else None,
        }


class ProductAttributeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductAttribute
        fields = ['id', 'source_id', 'name', 'raw_name', 'value', 'created_at']


class ProductCrossCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductCrossCode
        fields = [
            'id', 'source_id', 'manufacturer', 'code',
            'normalized_code', 'code_type', 'created_at',
        ]


class VehicleFitmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleFitment
        fields = [
            'id', 'source_id', 'source_url', 'make', 'model', 'generation', 'date_from',
            'date_to', 'modification', 'engine_code', 'power_hp',
            'raw_text', 'confidence', 'needs_review', 'review_status', 'reviewed_at', 'created_at',
        ]


class ProductEnrichmentFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductEnrichmentFact
        fields = [
            'id', 'source_id', 'source_url', 'fact_type', 'name', 'value',
            'raw_text', 'confidence', 'needs_review', 'review_status', 'reviewed_at', 'created_at',
        ]


class ProductParseJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductParseJob
        fields = [
            'id', 'product_id', 'brand', 'article', 'normalized_article',
            'source_id', 'source_url', 'status', 'error_message',
            'parsed_data', 'duration_ms', 'created_at', 'updated_at',
            'started_at', 'finished_at',
        ]


class ProductDetailSerializer(ProductSerializer):
    attributes = ProductAttributeSerializer(many=True, read_only=True)
    cross_codes = ProductCrossCodeSerializer(many=True, read_only=True)
    fitments = VehicleFitmentSerializer(many=True, read_only=True)
    enrichment_facts = ProductEnrichmentFactSerializer(many=True, read_only=True)
    latest_parse_job = serializers.SerializerMethodField()

    class Meta(ProductSerializer.Meta):
        fields = ProductSerializer.Meta.fields + [
            'attributes', 'cross_codes', 'fitments', 'enrichment_facts', 'latest_parse_job',
        ]

    def get_latest_parse_job(self, obj):
        from apps.products.source_policy import PART_SOURCE_POLICIES
        from django.db.models import Case, IntegerField, Value, When
        priority_cases = [
            When(source_id=sid, then=Value(policy.priority))
            for sid, policy in PART_SOURCE_POLICIES.items()
        ]
        job = (
            obj.parse_jobs
            .annotate(src_priority=Case(
                *priority_cases,
                default=Value(0),
                output_field=IntegerField(),
            ))
            .order_by('-src_priority', '-created_at')
            .first()
        )
        if job is None:
            return None
        return ProductParseJobSerializer(job).data


class ProductBulkActionJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductBulkActionJob
        fields = [
            'id', 'action', 'source_id', 'status', 'total_count',
            'queued_count', 'processed_count', 'success_count',
            'skipped_count', 'failed_count', 'batch_size', 'pause_seconds',
            'next_batch_at', 'error_message', 'created_at', 'updated_at',
            'started_at', 'finished_at',
        ]
