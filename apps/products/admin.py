from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from apps.products.models import (
    Product, ProductAttribute, ProductCrossCode, ProductEnrichmentFact,
    ProductImage, ProductParseJob, VehicleFitment,
)


class ProductImageInline(TabularInline):
    """Инлайн для просмотра изображений товара в карточке товара."""

    model = ProductImage
    extra = 0
    readonly_fields = ['sha256', 'url_source', 'uploaded_at', 'position']
    fields = ['position', 'url_source', 'sha256', 'uploaded_at']
    can_delete = False


class ProductAttributeInline(TabularInline):
    model = ProductAttribute
    extra = 0
    fields = ['source_id', 'name', 'value']
    readonly_fields = ['source_id', 'name', 'value']
    can_delete = False


class ProductCrossCodeInline(TabularInline):
    model = ProductCrossCode
    extra = 0
    fields = ['source_id', 'manufacturer', 'code', 'normalized_code', 'code_type']
    readonly_fields = ['source_id', 'manufacturer', 'code', 'normalized_code', 'code_type']
    can_delete = False


class VehicleFitmentInline(TabularInline):
    model = VehicleFitment
    extra = 0
    fields = [
        'source_id', 'make', 'model', 'generation', 'date_from', 'date_to',
        'modification', 'engine_code', 'power_hp', 'needs_review',
    ]
    readonly_fields = fields
    can_delete = False


class ProductEnrichmentFactInline(TabularInline):
    model = ProductEnrichmentFact
    extra = 0
    fields = ['source_id', 'fact_type', 'name', 'value', 'confidence', 'needs_review']
    readonly_fields = fields
    can_delete = False


@admin.register(Product)
class ProductAdmin(ModelAdmin):
    """
    Администрирование товаров.

    Actions позволяют оператору принудительно публиковать, архивировать
    и перегенерировать описание выбранных товаров.
    """

    list_display = ['article', 'name', 'tenant', 'price', 'stock_qty', 'export_enabled', 'sync_at']
    list_filter = ['tenant', 'export_enabled', 'condition']
    search_fields = ['article', 'name', 'brand']
    readonly_fields = ['hash_1c', 'sync_at', 'created_at', 'updated_at']
    inlines = [
        ProductImageInline,
        ProductAttributeInline,
        ProductCrossCodeInline,
        VehicleFitmentInline,
        ProductEnrichmentFactInline,
    ]
    actions = [
        'force_publish_selected',
        'force_archive_selected',
        'regenerate_description_selected',
        'parse_enrichment_selected',
    ]

    @admin.action(description='Принудительно опубликовать выбранные товары')
    def force_publish_selected(self, request, queryset):
        """Ставит задачи публикации для всех черновых/отклонённых листингов товара."""
        from apps.marketplaces.tasks import publish_listing_task

        queued = 0
        for product in queryset.prefetch_related('listings'):
            for listing in product.listings.filter(
                status__in=['draft', 'rejected', 'limit_reached'],
            ):
                publish_listing_task.delay(listing.pk)
                queued += 1
        self.message_user(request, f'Задачи публикации поставлены в очередь: {queued}.')

    @admin.action(description='Принудительно архивировать выбранные товары')
    def force_archive_selected(self, request, queryset):
        """Ставит задачи снятия с публикации для всех активных листингов товара."""
        from apps.marketplaces.tasks import unpublish_listing_task

        queued = 0
        for product in queryset.prefetch_related('listings'):
            for listing in product.listings.filter(status='active'):
                unpublish_listing_task.delay(listing.pk)
                queued += 1
        self.message_user(request, f'Задачи архивирования поставлены в очередь: {queued}.')

    @admin.action(description='Перегенерировать AI-описание для выбранных')
    def regenerate_description_selected(self, request, queryset):
        """Ставит задачи генерации AI-описания для выбранных товаров."""
        from apps.ai_agent.tasks import generate_description_task

        queued = 0
        for product in queryset:
            generate_description_task.delay(product.pk)
            queued += 1
        self.message_user(request, f'Задачи генерации описания поставлены в очередь: {queued}.')

    @admin.action(description='Обогатить данные выбранных товаров')
    def parse_enrichment_selected(self, request, queryset):
        """Ставит tenant-scoped задачи обогащения для выбранных товаров."""
        from apps.products.enrichment import normalize_part_code
        from apps.products.services import ProductEnrichmentService
        from apps.products.tasks import parse_single_part

        queued = 0
        for product in queryset.select_related('tenant'):
            job = ProductEnrichmentService.create_parse_job(
                tenant=product.tenant,
                product=product,
                brand=product.brand,
                article=product.article,
                normalized_article=normalize_part_code(product.article),
            )
            parse_single_part.delay(job.pk)
            queued += 1
        self.message_user(request, f'Задачи обогащения поставлены в очередь: {queued}.')


@admin.register(ProductParseJob)
class ProductParseJobAdmin(ModelAdmin):
    list_display = [
        'created_at', 'tenant', 'brand', 'article', 'source_id',
        'status', 'product', 'started_at', 'finished_at',
    ]
    list_filter = ['status', 'source_id', 'created_at', 'tenant']
    search_fields = ['brand', 'article', 'normalized_article', 'product__name']
    readonly_fields = [
        'tenant', 'product', 'brand', 'article', 'normalized_article',
        'source_id', 'source_url', 'status', 'error_message', 'raw_html',
        'raw_text', 'parsed_data', 'duration_ms', 'created_at', 'updated_at',
        'started_at', 'finished_at',
    ]

    def has_add_permission(self, request):
        return False
