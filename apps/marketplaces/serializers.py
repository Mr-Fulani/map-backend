from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers

from apps.marketplaces.listing_delivery import (
    listing_delivery_presentation,
    listing_publication_available,
)
from apps.marketplaces.autoload_onboarding import (
    autoload_onboarding_presentation,
)
from apps.marketplaces.models import (
    AvitoAccountStatus,
    AvitoCategory,
    CategoryMapping,
    Listing,
    MarketplaceAccount,
    MarketplacePlacementAddress,
)
from apps.products.media import (
    get_product_image_delivery_key, get_publishable_product_images,
)


class AvitoAccountStatusSerializer(serializers.ModelSerializer):
    """Tenant-facing состояние подключения, Автозагрузки и тарифа Avito."""

    days_left = serializers.SerializerMethodField()
    subscription_ends_at = serializers.SerializerMethodField()
    subscription_source = serializers.SerializerMethodField()
    placements_remaining = serializers.SerializerMethodField()
    placements_total = serializers.SerializerMethodField()
    profile_stale = serializers.SerializerMethodField()
    tariff_stale = serializers.SerializerMethodField()

    class Meta:
        model = AvitoAccountStatus
        fields = [
            'connection_status', 'autoload_status', 'feed_configured',
            'profile_checked_at', 'profile_stale',
            'tariff_status', 'tariff_name', 'tariff_started_at',
            'tariff_ends_at', 'subscription_ends_at', 'subscription_source',
            'days_left', 'tariff_price',
            'placement_packages', 'placements_remaining', 'placements_total',
            'scheduled_tariff', 'tariff_checked_at', 'tariff_stale',
            'last_attempted_at', 'last_error_code', 'last_error_message',
        ]
        read_only_fields = fields

    def get_days_left(self, obj) -> int | None:
        """Возвращает дни по API-тарифу или по ручной дате Autoload."""
        if obj.tariff_ends_at:
            seconds_left = (obj.tariff_ends_at - timezone.now()).total_seconds()
            if seconds_left <= 0:
                return 0
            return int((seconds_left + 86399) // 86400)
        manual_end = obj.account.autoload_subscription_ends_at
        if not manual_end:
            return None
        return max((manual_end - timezone.localdate()).days, 0)

    @extend_schema_field(serializers.DateField(allow_null=True, read_only=True))
    def get_subscription_ends_at(self, obj) -> str | None:
        """Единая дата окончания для интерфейса."""
        if obj.tariff_ends_at:
            return timezone.localtime(obj.tariff_ends_at).date().isoformat()
        manual_end = obj.account.autoload_subscription_ends_at
        return manual_end.isoformat() if manual_end else None

    def get_subscription_source(self, obj) -> str:
        """Показывает, подтверждена дата API или указана пользователем."""
        if obj.tariff_ends_at:
            return 'avito_tariff'
        if obj.account.autoload_subscription_ends_at:
            return 'manual'
        return 'unavailable'

    def get_placements_remaining(self, obj) -> int | None:
        """Суммирует известные остатки по пакетам размещений."""
        values: list[int] = []
        for package in obj.placement_packages:
            if not isinstance(package, dict):
                continue
            value = package.get('remain')
            if isinstance(value, int):
                values.append(value)
        return sum(values) if values else None

    def get_placements_total(self, obj) -> int | None:
        """Суммирует размеры известных пакетов размещений."""
        values: list[int] = []
        for package in obj.placement_packages:
            if not isinstance(package, dict):
                continue
            value = package.get('total')
            if isinstance(value, int):
                values.append(value)
        return sum(values) if values else None

    @staticmethod
    def _is_stale(checked_at) -> bool:
        if not checked_at:
            return True
        return checked_at < timezone.now() - timedelta(hours=12)

    def get_profile_stale(self, obj) -> bool:
        """Показывает, что профиль не подтверждался более 12 часов."""
        return self._is_stale(obj.profile_checked_at)

    def get_tariff_stale(self, obj) -> bool:
        """Показывает, что тариф не подтверждался более 12 часов."""
        return self._is_stale(obj.tariff_checked_at)


class AvitoCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = AvitoCategory
        fields = ['avito_id', 'name', 'parent_id', 'is_leaf']


class CategoryMappingSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryMapping
        fields = ['id', 'marketplace', 'category_source', 'category_target',
                  'category_id', 'attributes_map', 'version', 'created_at']
        read_only_fields = ['version', 'created_at']


class CategoryMappingWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = CategoryMapping
        fields = ['category_source', 'category_target', 'category_id', 'attributes_map']


class AutoloadOnboardingSerializer(serializers.Serializer):
    """Tenant-safe state of MAP's managed Autoload endpoint setup."""

    state = serializers.CharField(read_only=True)
    profile_state = serializers.CharField(read_only=True)
    ready = serializers.BooleanField(read_only=True)
    retryable = serializers.BooleanField(read_only=True)
    message = serializers.CharField(read_only=True)


class MarketplaceCapabilitiesSerializer(serializers.Serializer):
    """Provider capabilities used to render only applicable UI controls."""

    account_health = serializers.BooleanField(read_only=True)
    publication = serializers.BooleanField(read_only=True)
    status_check = serializers.BooleanField(read_only=True)
    analytics = serializers.BooleanField(read_only=True)
    feed_delivery = serializers.BooleanField(read_only=True)
    placement_addresses = serializers.BooleanField(read_only=True)


def marketplace_capabilities(marketplace: str) -> dict[str, bool]:
    """Return a fail-closed presentation contract for one provider."""
    is_avito = marketplace == MarketplaceAccount.MARKETPLACE_AVITO
    return {
        'account_health': is_avito,
        'publication': is_avito,
        'status_check': is_avito,
        'analytics': is_avito,
        'feed_delivery': is_avito,
        'placement_addresses': is_avito,
    }


def marketplace_label(marketplace: str) -> str:
    labels = dict(MarketplaceAccount.MARKETPLACE_CHOICES)
    return labels.get(marketplace, marketplace.capitalize())


class MarketplaceAccountSerializer(serializers.ModelSerializer):
    """Чтение: credentials не возвращаются никогда."""

    marketplace_label = serializers.SerializerMethodField()
    provider_capabilities = serializers.SerializerMethodField()
    avito_status = serializers.SerializerMethodField()
    feed_endpoint_managed = serializers.SerializerMethodField()
    autoload_onboarding = serializers.SerializerMethodField()

    class Meta:
        model = MarketplaceAccount
        fields = [
            'id', 'name', 'marketplace', 'marketplace_label',
            'provider_capabilities', 'external_id', 'is_active',
            'default_address', 'default_seller_address_id',
            'default_manager_name', 'default_contact_phone',
            'autoload_active', 'autoload_checked_at',
            'autoload_subscription_ends_at',
            'feed_endpoint_managed',
            'autoload_onboarding',
            'avito_status',
            'created_at',
        ]
        read_only_fields = ['created_at', 'autoload_active', 'autoload_checked_at']

    def get_marketplace_label(self, obj) -> str:
        return marketplace_label(obj.marketplace)

    @extend_schema_field(MarketplaceCapabilitiesSerializer)
    def get_provider_capabilities(self, obj) -> dict[str, bool]:
        return marketplace_capabilities(obj.marketplace)

    @extend_schema_field(AvitoAccountStatusSerializer(allow_null=True))
    def get_avito_status(self, obj):
        """Возвращает последний снимок Avito без внешнего запроса."""
        if obj.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return None
        try:
            status_obj = obj.avito_status
        except AvitoAccountStatus.DoesNotExist:
            status_obj = AvitoAccountStatus(account=obj, tenant=obj.tenant)
        return AvitoAccountStatusSerializer(status_obj).data

    def get_feed_endpoint_managed(self, obj) -> bool:
        """Сообщает UI, что URL защищён и полностью управляется MAP."""
        return (
            obj.marketplace == MarketplaceAccount.MARKETPLACE_AVITO
            and hasattr(obj, 'feed_endpoint')
        )

    @extend_schema_field(AutoloadOnboardingSerializer(allow_null=True))
    def get_autoload_onboarding(self, obj):
        """Отделяет готовность endpoint MAP от тарифа Avito."""
        if obj.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return None
        presentation = autoload_onboarding_presentation(obj)
        return AutoloadOnboardingSerializer(presentation).data


class MarketplacePlacementAddressSerializer(serializers.ModelSerializer):
    account_name = serializers.CharField(source='account.name', read_only=True)

    class Meta:
        model = MarketplacePlacementAddress
        fields = [
            'id', 'account', 'account_name', 'name', 'seller_address_id', 'address',
            'manager_name', 'contact_phone', 'is_default', 'is_active',
            'created_at',
        ]
        read_only_fields = ['created_at']


class ListingSerializer(serializers.ModelSerializer):
    """Листинг для Dashboard — без credentials, с denormalized полями."""

    product_id = serializers.IntegerField(source='product.pk', read_only=True)
    product_article = serializers.CharField(source='product.article', read_only=True)
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_brand = serializers.CharField(source='product.brand', read_only=True)
    account_id = serializers.IntegerField(source='account.pk', read_only=True)
    account_name = serializers.CharField(source='account.name', read_only=True)
    marketplace = serializers.CharField(source='account.marketplace', read_only=True)
    marketplace_label = serializers.SerializerMethodField()
    provider_capabilities = serializers.SerializerMethodField()
    status_display = serializers.SerializerMethodField()
    status_explanation = serializers.SerializerMethodField()
    delivery_stage = serializers.SerializerMethodField()
    provider_submission_started = serializers.SerializerMethodField()
    lifecycle_actions_blocked = serializers.SerializerMethodField()
    can_check_avito_status = serializers.SerializerMethodField()
    can_check_provider_status = serializers.SerializerMethodField()
    can_publish = serializers.SerializerMethodField()
    rejection_ready_to_retry = serializers.SerializerMethodField()
    delivery_retry_at = serializers.SerializerMethodField()
    delivery_retry_reason = serializers.SerializerMethodField()

    class Meta:
        model = Listing
        fields = [
            'id', 'status', 'status_display', 'status_explanation',
            'delivery_stage',
            'provider_submission_started', 'lifecycle_actions_blocked',
            'can_check_avito_status', 'can_check_provider_status',
            'delivery_retry_at',
            'delivery_retry_reason', 'can_publish',
            'rejection_ready_to_retry',
            'product_id', 'product_article', 'product_name', 'product_brand',
            'account_id', 'account_name', 'marketplace', 'marketplace_label',
            'provider_capabilities',
            'title', 'price_on_listing', 'external_id', 'external_url',
            'ad_type',
            'placement_address',
            'address_override', 'seller_address_id_override',
            'manager_name_override', 'contact_phone_override',
            'bulk_placement_address',
            'bulk_address', 'bulk_seller_address_id',
            'bulk_manager_name', 'bulk_contact_phone',
            'rejection_reason', 'retry_count', 'published_at', 'last_sync_at',
            'remote_status', 'remote_status_checked_at',
            'next_status_check_at',
            'created_at',
        ]
        read_only_fields = fields

    def get_status_display(self, obj) -> str:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return {
                Listing.STATUS_DRAFT: 'Черновик',
                Listing.STATUS_QUEUED: 'В очереди',
                Listing.STATUS_PENDING: 'Обрабатывается площадкой',
                Listing.STATUS_ACTIVE: 'Активно',
                Listing.STATUS_REJECTED: 'Отклонено',
                Listing.STATUS_ARCHIVING: 'Снимается с публикации',
                Listing.STATUS_ARCHIVED: 'В архиве',
                Listing.STATUS_DELETED: 'Удалено',
                Listing.STATUS_REQUIRES_REVIEW: 'Требует проверки',
                Listing.STATUS_LIMIT_REACHED: 'Лимит достигнут',
            }.get(obj.status, obj.status)
        if self._rejection_ready_to_retry(obj):
            return 'Исправлено — отправьте снова'
        return listing_delivery_presentation(obj).label

    def get_status_explanation(self, obj) -> str:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return ''
        if obj.status == Listing.STATUS_ACTIVE:
            return (
                'Avito подтверждает, что объявление активно. MAP продолжает '
                'автоматически сверять его статус.'
            )
        if obj.status == Listing.STATUS_ARCHIVING:
            return (
                'MAP уже исключил объявление из актуального XML-фида. '
                'Ожидаем, когда Avito подтвердит снятие с публикации.'
            )
        if obj.status == Listing.STATUS_ARCHIVED:
            if (
                obj.remote_status == Listing.REMOTE_STATUS_REMOVED
                and not obj.external_id
            ):
                return (
                    'Avito больше не находит это объявление. Оно исключено '
                    'из фида; повторная публикация создаст новое объявление.'
                )
            if obj.remote_status in {
                Listing.REMOTE_STATUS_REMOVED,
                Listing.REMOTE_STATUS_ARCHIVED,
            }:
                return (
                    'Avito подтвердил, что объявление больше не активно. '
                    'Оно исключено из актуального XML-фида.'
                )
            return 'Объявление не входит в актуальный XML-фид Avito.'
        if self._rejection_ready_to_retry(obj):
            return (
                'Предыдущая версия была отклонена Avito. Текущие поля '
                'прошли проверку MAP. Нажмите «Опубликовать», чтобы '
                'отправить исправленную версию.'
            )
        if obj.status == Listing.STATUS_REJECTED:
            return (
                'Avito отклонил или заблокировал объявление. Исправьте '
                'подсвеченные поля и нажмите «Опубликовать». Кнопка проверки '
                'только запрашивает текущий статус и не отправляет изменения.'
            )
        return ''

    def get_delivery_stage(self, obj) -> str:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return 'unavailable'
        return listing_delivery_presentation(obj).stage

    def get_provider_submission_started(self, obj) -> bool:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return False
        return listing_delivery_presentation(obj).provider_submission_started

    def get_lifecycle_actions_blocked(self, obj) -> bool:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return True
        return listing_delivery_presentation(obj).lifecycle_actions_blocked

    def get_can_check_avito_status(self, obj) -> bool:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return False
        return listing_delivery_presentation(obj).can_check_avito_status

    def get_can_check_provider_status(self, obj) -> bool:
        return self.get_can_check_avito_status(obj)

    def get_can_publish(self, obj) -> bool:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return False
        return listing_publication_available(obj)

    def get_rejection_ready_to_retry(self, obj) -> bool:
        """Whether Publish would accept the corrected rejected listing now."""
        return self._rejection_ready_to_retry(obj)

    def _rejection_ready_to_retry(self, obj) -> bool:
        if (
            obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO
            or obj.status != Listing.STATUS_REJECTED
            or not listing_publication_available(obj)
        ):
            return False
        try:
            errors, _warnings = self._get_avito_preflight(obj)
        except Exception:
            return False
        return not errors

    def _get_avito_preflight(self, obj) -> tuple[dict, dict]:
        from apps.marketplaces.adapters.avito.feed_builder import (
            avito_publication_preflight,
        )
        cache = getattr(self, '_avito_preflight_cache', None)
        if cache is None:
            cache = {}
            self._avito_preflight_cache = cache
        key = getattr(obj, 'pk', id(obj))
        if key not in cache:
            cache[key] = avito_publication_preflight(obj)
        return cache[key]

    @extend_schema_field(serializers.DateTimeField(allow_null=True, read_only=True))
    def get_delivery_retry_at(self, obj) -> str | None:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return None
        retry_at = listing_delivery_presentation(obj).retry_at
        if retry_at is None:
            return None
        return serializers.DateTimeField().to_representation(retry_at)

    def get_delivery_retry_reason(self, obj) -> str:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return ''
        return listing_delivery_presentation(obj).retry_reason

    def get_marketplace_label(self, obj) -> str:
        return marketplace_label(obj.account.marketplace)

    @extend_schema_field(MarketplaceCapabilitiesSerializer)
    def get_provider_capabilities(self, obj) -> dict[str, bool]:
        return marketplace_capabilities(obj.account.marketplace)


def _image_url(s3_key: str, fallback: str, request=None) -> str:
    """Строит URL изображения: CDN (только при S3) → default_storage → fallback."""
    from django.core.files.storage import default_storage
    cdn = getattr(settings, 'YC_CDN_DOMAIN', '')
    # CDN используем только если storage реально S3 (FileSystemStorage не имеет bucket_name)
    is_s3 = hasattr(default_storage, 'bucket_name')
    if cdn and s3_key and is_s3:
        return f'https://{cdn}/{s3_key}'
    if s3_key:
        url = default_storage.url(s3_key)
        if url.startswith('/') and request:
            return request.build_absolute_uri(url)
        return url
    return fallback or ''


class ListingDetailSerializer(ListingSerializer):
    """
    Расширенный сериализатор листинга для дровера предпросмотра.

    Добавляет AI-поля и список изображений товара.
    """

    description_ai = serializers.CharField(read_only=True)
    ai_confidence = serializers.FloatField(read_only=True)
    ai_confidence_display = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()
    catalog_category = serializers.SerializerMethodField()
    base_price = serializers.DecimalField(source='product.price', max_digits=12, decimal_places=2, read_only=True)
    product_oem_numbers = serializers.ListField(
        source='product.oem_numbers',
        child=serializers.CharField(),
        read_only=True,
    )
    product_avito_oem = serializers.SerializerMethodField()
    avito_field_warnings = serializers.SerializerMethodField()
    avito_field_errors = serializers.SerializerMethodField()
    avito_field_warnings_by_field = serializers.SerializerMethodField()
    avito_brand_valid = serializers.SerializerMethodField()
    avito_brand_catalog_synced_at = serializers.SerializerMethodField()

    class Meta(ListingSerializer.Meta):
        fields = ListingSerializer.Meta.fields + [
            'description_ai', 'ai_confidence', 'ai_confidence_display', 'images',
            'catalog_category', 'margin_pct', 'base_price', 'product_oem_numbers',
            'product_avito_oem',
            'avito_field_warnings',
            'avito_field_errors', 'avito_field_warnings_by_field',
            'avito_brand_valid', 'avito_brand_catalog_synced_at',
        ]
        read_only_fields = fields

    def get_product_avito_oem(self, obj) -> str:
        """Exact single OEM value the current feed builder will emit."""
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return ''
        from apps.marketplaces.adapters.avito.feed_builder import _get_oem
        return _get_oem(obj)

    @extend_schema_field(serializers.ListField(
        child=serializers.CharField(), read_only=True,
    ))
    def get_avito_field_warnings(self, obj) -> list:
        """Неблокирующие рекомендации Avito, видимые тенанту до публикации."""
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return []
        try:
            _errors, warnings = self._get_avito_preflight(obj)
            return [
                message
                for messages in warnings.values()
                for message in messages
            ]
        except Exception:
            return []

    @extend_schema_field(serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()), read_only=True,
    ))
    def get_avito_field_errors(self, obj) -> dict:
        """Blocking publication errors keyed by the editable drawer field."""
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return {}
        try:
            errors, _warnings = self._get_avito_preflight(obj)
            return errors
        except Exception:
            return {}

    @extend_schema_field(serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()), read_only=True,
    ))
    def get_avito_field_warnings_by_field(self, obj) -> dict:
        """Non-blocking publication warnings keyed by drawer field."""
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return {}
        try:
            _errors, warnings = self._get_avito_preflight(obj)
            return warnings
        except Exception:
            return {}

    def get_avito_brand_valid(self, obj) -> bool:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return False
        from apps.marketplaces.adapters.avito.feed_builder import (
            product_brand_is_missing,
            unknown_brand_details,
        )
        return (
            not product_brand_is_missing(obj)
            and unknown_brand_details(obj) is None
        )

    @extend_schema_field(serializers.DateTimeField(allow_null=True, read_only=True))
    def get_avito_brand_catalog_synced_at(self, obj) -> str | None:
        if obj.account.marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
            return None
        from apps.marketplaces.adapters.avito.brand_catalog import catalog_status
        synced_at = catalog_status()['synced_at']
        return synced_at.isoformat() if synced_at else None

    @extend_schema_field(inline_serializer(
        name='ListingCatalogCategory',
        allow_null=True,
        fields={
            'id': serializers.IntegerField(read_only=True),
            'name': serializers.CharField(read_only=True),
            'parent_id': serializers.IntegerField(
                allow_null=True, read_only=True,
            ),
            'parent_name': serializers.CharField(
                allow_null=True, read_only=True,
            ),
            'default_margin_pct': serializers.DecimalField(
                max_digits=5,
                decimal_places=2,
                allow_null=True,
                read_only=True,
            ),
        },
    ))
    def get_catalog_category(self, obj):
        """Текущая категория каталога товара (для перепроверки/смены в дровере)."""
        category = getattr(obj.product, 'catalog_category', None)
        if not category:
            return None
        return {
            'id': category.id,
            'name': category.name,
            'parent_id': category.parent_id,
            'parent_name': category.parent.name if category.parent_id else None,
            'default_margin_pct': (
                str(category.default_margin_pct)
                if category.default_margin_pct is not None
                else None
            ),
        }

    def get_ai_confidence_display(self, obj) -> str:
        """Возвращает уверенность AI в виде строки с процентами."""
        if obj.ai_confidence is None:
            return '—'
        pct = round(obj.ai_confidence * 100)
        if pct >= 70:
            label = 'Высокая'
        elif pct >= 50:
            label = 'Средняя'
        else:
            label = 'Низкая'
        return f'{label} ({pct}%)'

    @extend_schema_field(inline_serializer(
        name='ListingImage',
        many=True,
        fields={
            'id': serializers.IntegerField(allow_null=True),
            'url': serializers.URLField(),
            'thumb_url': serializers.URLField(),
            'position': serializers.IntegerField(),
            'is_primary': serializers.BooleanField(),
            'source': serializers.CharField(required=False),
        },
    ))
    def get_images(self, obj) -> list:
        """Возвращает список изображений товара с CDN-ссылками."""
        request = self.context.get('request')
        images = [
            {
                'id': img.pk,
                'url': _image_url(
                    get_product_image_delivery_key(img), img.url_source, request,
                ),
                'thumb_url': _image_url(img.s3_key_thumb, img.url_source, request),
                'position': img.position,
                'is_primary': img.is_primary,
            }
            for img in get_publishable_product_images(obj.product)
        ]
        if images:
            return images
        category = getattr(obj.product, 'catalog_category', None)
        if category and category.default_image_s3_key:
            url = _image_url(category.default_image_s3_key, '', request)
            return [{
                'id': None,
                'url': url,
                'thumb_url': url,
                'position': 0,
                'is_primary': True,
                'source': 'category_default',
            }]
        return []


class MarketplaceAccountWriteSerializer(serializers.Serializer):
    """Запись: принимает client_id/client_secret, шифрует через Fernet."""

    name = serializers.CharField(max_length=200)
    marketplace = serializers.ChoiceField(
        choices=MarketplaceAccount.MARKETPLACE_CHOICES,
        default=MarketplaceAccount.MARKETPLACE_AVITO,
    )
    external_id = serializers.CharField(max_length=100)
    client_id = serializers.CharField(write_only=True)
    client_secret = serializers.CharField(write_only=True)


class MarketplaceAccountPlacementSerializer(serializers.Serializer):
    default_address = serializers.CharField(max_length=500, required=False, allow_blank=True)
    default_seller_address_id = serializers.CharField(max_length=100, required=False, allow_blank=True)
    default_manager_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    default_contact_phone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    autoload_subscription_ends_at = serializers.DateField(required=False, allow_null=True)


class ListingPlacementSerializer(serializers.Serializer):
    placement_address = serializers.IntegerField(required=False, allow_null=True)
    address_override = serializers.CharField(max_length=500, required=False, allow_blank=True)
    seller_address_id_override = serializers.CharField(max_length=100, required=False, allow_blank=True)
    manager_name_override = serializers.CharField(max_length=100, required=False, allow_blank=True)
    contact_phone_override = serializers.CharField(max_length=50, required=False, allow_blank=True)


class ListingFieldsSerializer(serializers.Serializer):
    account_id = serializers.IntegerField(required=False)
    price_on_listing = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        required=False,
        min_value=Decimal('0'),
    )
    margin_pct = serializers.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        allow_null=True,
        min_value=Decimal('0'),
    )
    ad_type = serializers.ChoiceField(choices=Listing.AD_TYPE_CHOICES, required=False)


class ListingBulkPlacementSerializer(ListingPlacementSerializer):
    listing_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=False,
        max_length=settings.API_BULK_MAX_ITEMS,
    )
    account_id = serializers.IntegerField(required=False)
    status = serializers.CharField(max_length=20, required=False, allow_blank=True)
    category_source = serializers.CharField(max_length=300, required=False, allow_blank=True)
    catalog_category_id = serializers.IntegerField(required=False)

    def validate(self, attrs):
        has_filter = any(
            attrs.get(field)
            for field in ('listing_ids', 'account_id', 'status', 'category_source', 'catalog_category_id')
        )
        if not has_filter:
            raise serializers.ValidationError('Укажите хотя бы один фильтр для массового обновления.')
        return attrs


class ListingBulkActionSerializer(ListingPlacementSerializer):
    ACTION_PUBLISH = 'publish'
    ACTION_ARCHIVE = 'archive'
    ACTION_DELETE = 'delete'
    ACTION_UPDATE_PLACEMENT = 'update_placement'
    ACTION_CHOICES = (
        ACTION_PUBLISH,
        ACTION_ARCHIVE,
        ACTION_DELETE,
        ACTION_UPDATE_PLACEMENT,
    )

    action = serializers.ChoiceField(choices=ACTION_CHOICES)
    listing_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=False,
        max_length=settings.API_BULK_MAX_ITEMS,
    )
    account_id = serializers.IntegerField(required=False)
    status = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate(self, attrs):
        has_filter = any(attrs.get(field) for field in ('listing_ids', 'account_id', 'status'))
        if not has_filter:
            raise serializers.ValidationError('Укажите хотя бы один фильтр для массового действия.')

        placement_fields = (
            'placement_address',
            'address_override',
            'seller_address_id_override',
            'manager_name_override',
            'contact_phone_override',
        )
        if attrs.get('action') == self.ACTION_UPDATE_PLACEMENT:
            if not any(field in attrs for field in placement_fields):
                raise serializers.ValidationError('Укажите поля размещения для массового обновления.')
        return attrs
