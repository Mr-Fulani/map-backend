from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline

from apps.products.models import Product, ProductImage


class ProductImageInline(TabularInline):
    """Инлайн для просмотра изображений товара в карточке товара."""

    model = ProductImage
    extra = 0
    readonly_fields = ['sha256', 'url_source', 'uploaded_at', 'position']
    fields = ['position', 'url_source', 'sha256', 'uploaded_at']
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
    inlines = [ProductImageInline]
    actions = ['force_publish_selected', 'force_archive_selected', 'regenerate_description_selected']

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
