import datetime
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from apps.tenants.api_views import ListingsAPIView as APIView

from apps.marketplaces.models import (
    CategoryMapping,
    Listing,
    ListingStats,
    MarketplaceAccount,
    MarketplacePlacementAddress,
)
from apps.marketplaces.serializers import (
    CategoryMappingSerializer,
    CategoryMappingWriteSerializer,
    AvitoAccountStatusSerializer,
    ListingDetailSerializer,
    ListingFieldsSerializer,
    ListingBulkPlacementSerializer,
    ListingBulkActionSerializer,
    ListingPlacementSerializer,
    ListingSerializer,
    MarketplaceAccountPlacementSerializer,
    MarketplaceAccountSerializer,
    MarketplaceAccountWriteSerializer,
    MarketplacePlacementAddressSerializer,
)
from apps.core.pagination import MapPagination
from apps.marketplaces.services import (
    AccountAlreadyExists,
    AvitoAccountStatusService,
    CategoryMappingService,
    InvalidMarketplaceCredentials,
    ListingBulkLimitExceeded,
    ListingAccountConflict,
    InvalidListingStatus,
    ListingNotFound,
    ListingService,
    MarketplaceAccountService,
)
from apps.tenants.permissions import TenantAdminPermission, TenantAdminWritePermission


def _ok_response(name, data):
    """Build the common MAP response envelope for OpenAPI only."""
    return inline_serializer(
        name=name,
        fields={
            'status': serializers.CharField(read_only=True),
            'data': data,
        },
    )


LISTING_UPDATE_REQUEST = inline_serializer(
    name='ListingUpdateRequest',
    fields={
        'title': serializers.CharField(required=False, allow_null=True),
        'description_ai': serializers.CharField(required=False, allow_null=True),
        'account_id': serializers.IntegerField(required=False),
        'price_on_listing': serializers.DecimalField(
            max_digits=12,
            decimal_places=2,
            required=False,
            min_value=Decimal('0'),
        ),
        'margin_pct': serializers.DecimalField(
            max_digits=5,
            decimal_places=2,
            required=False,
            allow_null=True,
            min_value=Decimal('0'),
        ),
        'ad_type': serializers.ChoiceField(
            choices=Listing.AD_TYPE_CHOICES, required=False,
        ),
        'placement_address': serializers.IntegerField(
            required=False, allow_null=True,
        ),
        'address_override': serializers.CharField(
            max_length=500, required=False, allow_blank=True,
        ),
        'seller_address_id_override': serializers.CharField(
            max_length=100, required=False, allow_blank=True,
        ),
        'manager_name_override': serializers.CharField(
            max_length=100, required=False, allow_blank=True,
        ),
        'contact_phone_override': serializers.CharField(
            max_length=50, required=False, allow_blank=True,
        ),
    },
)


@extend_schema(tags=['Accounts'])
class MarketplaceAccountListView(APIView):
    """GET /api/v1/accounts/ — список аккаунтов. POST — создать."""

    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(
        operation_id='marketplace_account_list',
        responses=MarketplaceAccountSerializer(many=True),
    )
    def get(self, request):
        """Возвращает аккаунты маркетплейсов текущего тенанта."""
        qs = MarketplaceAccount.objects.filter(
            tenant=request.tenant,
        ).select_related('avito_status')
        return Response(MarketplaceAccountSerializer(qs, many=True).data)

    @extend_schema(
        operation_id='marketplace_account_create',
        request=MarketplaceAccountWriteSerializer,
        responses={201: MarketplaceAccountSerializer},
    )
    def post(self, request):
        """Создаёт аккаунт Avito, делегируя логику MarketplaceAccountService.create."""
        serializer = MarketplaceAccountWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            account = MarketplaceAccountService.create(request.tenant, serializer.validated_data)
        except AccountAlreadyExists as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        except InvalidMarketplaceCredentials as exc:
            return Response({
                'status': 'error',
                'code': 'validation_error',
                'message': str(exc),
            }, status=status.HTTP_400_BAD_REQUEST)
        return Response(MarketplaceAccountSerializer(account).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Accounts'])
class MarketplaceAccountDetailView(APIView):
    """GET/PUT/DELETE /api/v1/accounts/{id}/"""

    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    def _get_account(self, pk, tenant):
        """Возвращает аккаунт тенанта или 404."""
        try:
            return MarketplaceAccount.objects.select_related(
                'avito_status',
            ).get(pk=pk, tenant=tenant)
        except MarketplaceAccount.DoesNotExist:
            return None

    @extend_schema(
        operation_id='marketplace_account_retrieve',
        responses=MarketplaceAccountSerializer,
    )
    def get(self, request, pk):
        """Детали аккаунта."""
        account = self._get_account(pk, request.tenant)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(MarketplaceAccountSerializer(account).data)

    @extend_schema(
        operation_id='marketplace_account_update',
        request=MarketplaceAccountWriteSerializer,
        responses=MarketplaceAccountSerializer,
    )
    def put(self, request, pk):
        """Обновляет аккаунт, делегируя логику MarketplaceAccountService.update_credentials."""
        account = self._get_account(pk, request.tenant)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = MarketplaceAccountWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            account = MarketplaceAccountService.update_credentials(account, serializer.validated_data)
        except AccountAlreadyExists as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_409_CONFLICT)
        except InvalidMarketplaceCredentials as exc:
            return Response({
                'status': 'error',
                'code': 'validation_error',
                'message': str(exc),
            }, status=status.HTTP_400_BAD_REQUEST)
        return Response(MarketplaceAccountSerializer(account).data)

    @extend_schema(
        operation_id='marketplace_account_partial_update',
        request=inline_serializer(
            name='MarketplaceAccountPatchRequest',
            fields={
                'name': serializers.CharField(max_length=200, required=False),
                'is_active': serializers.BooleanField(required=False),
                'default_address': serializers.CharField(
                    max_length=500, required=False, allow_blank=True,
                ),
                'default_seller_address_id': serializers.CharField(
                    max_length=100, required=False, allow_blank=True,
                ),
                'default_manager_name': serializers.CharField(
                    max_length=100, required=False, allow_blank=True,
                ),
                'default_contact_phone': serializers.CharField(
                    max_length=50, required=False, allow_blank=True,
                ),
                'autoload_subscription_ends_at': serializers.DateField(
                    required=False, allow_null=True,
                ),
            },
        ),
        responses=MarketplaceAccountSerializer,
    )
    def patch(self, request, pk):
        """Частичное обновление аккаунта, делегируя логику MarketplaceAccountService.update_partial."""
        account = self._get_account(pk, request.tenant)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        placement_fields = {
            field: request.data[field]
            for field in (
                'default_address',
                'default_seller_address_id',
                'default_manager_name',
                'default_contact_phone',
                'autoload_subscription_ends_at',
            )
            if field in request.data
        }
        serializer = MarketplaceAccountPlacementSerializer(data=placement_fields, partial=True)
        serializer.is_valid(raise_exception=True)
        data = {**request.data, **serializer.validated_data}
        account = MarketplaceAccountService.update_partial(account, data)
        return Response(MarketplaceAccountSerializer(account).data)

    @extend_schema(
        operation_id='marketplace_account_delete',
        request=None,
        responses={204: None},
    )
    def delete(self, request, pk):
        """Мягко удаляет аккаунт и его листинги до retention purge."""
        account = self._get_account(pk, request.tenant)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            Listing.objects.filter(account=account, tenant=request.tenant).delete()
            account.soft_delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Accounts'])
class AutoloadStatusView(APIView):
    """GET /api/v1/accounts/{id}/autoload-status/ — проверить активирована ли Автозагрузка Avito."""

    permission_classes = [IsAuthenticated, TenantAdminPermission]

    @extend_schema(
        operation_id='marketplace_account_autoload_status',
        responses=inline_serializer(
            name='MarketplaceAccountAutoloadStatusResponse',
            fields={
                'activated': serializers.BooleanField(read_only=True),
                'feed_url': serializers.URLField(read_only=True),
                'stale': serializers.BooleanField(read_only=True),
                'status': AvitoAccountStatusSerializer(read_only=True),
                'activate_url': serializers.URLField(
                    required=False,
                ),
            },
        ),
    )
    def get(self, request, pk):
        """
        Проверяет активирован ли Avito Autoload для аккаунта.

        Возвращает:
          {"activated": true, "feed_url": "https://..."}  — если профиль найден
          {"activated": false, "feed_url": "https://...", "activate_url": "https://www.avito.ru/autoload/settings"}
        """
        try:
            account = MarketplaceAccount.objects.get(pk=pk, tenant=request.tenant)
        except MarketplaceAccount.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        from apps.marketplaces.adapters.avito.adapter import AvitoAdapter

        status_obj = AvitoAccountStatusService.refresh(account)
        snapshot = AvitoAccountStatusSerializer(status_obj).data
        activated = status_obj.autoload_status == status_obj.AUTOLOAD_ENABLED
        payload = {
            'activated': activated,
            'feed_url': AvitoAdapter(account)._feed_public_url(),
            'stale': snapshot['profile_stale'],
            'status': snapshot,
        }
        if not activated:
            payload['activate_url'] = 'https://www.avito.ru/autoload/settings'
        return Response(payload)


@extend_schema(tags=['Accounts'])
class MarketplacePlacementAddressListView(APIView):
    """GET/POST /api/v1/accounts/placement-addresses/ — справочник адресов размещения."""

    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(
        operation_id='marketplace_placement_address_list',
        parameters=[
            OpenApiParameter(
                'account', OpenApiTypes.INT, OpenApiParameter.QUERY,
                description='Фильтр по ID аккаунта маркетплейса.',
            ),
        ],
        responses=MarketplacePlacementAddressSerializer(many=True),
    )
    def get(self, request):
        # Только активные адреса: удалённые (soft-delete, is_active=False) не должны
        # попадать в выпадающий список, иначе их можно выбрать, но сохранение листинга
        # их отвергнет (см. _get_placement_address) — расхождение давало 404 при правке.
        qs = MarketplacePlacementAddress.objects.filter(
            tenant=request.tenant, is_active=True
        ).select_related('account')
        account_id = request.query_params.get('account', '').strip()
        if account_id:
            qs = qs.filter(account_id=account_id)
        return Response(MarketplacePlacementAddressSerializer(qs, many=True).data)

    @extend_schema(
        operation_id='marketplace_placement_address_create',
        request=MarketplacePlacementAddressSerializer,
        responses={201: MarketplacePlacementAddressSerializer},
    )
    def post(self, request):
        serializer = MarketplacePlacementAddressSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            account = MarketplaceAccount.objects.get(pk=data['account'].pk, tenant=request.tenant)
        except MarketplaceAccount.DoesNotExist:
            return Response({'detail': 'Аккаунт Avito не найден'}, status=status.HTTP_404_NOT_FOUND)
        if data.get('is_default'):
            MarketplacePlacementAddress.objects.filter(
                tenant=request.tenant,
                account=account,
                is_default=True,
            ).update(is_default=False)
        address = MarketplacePlacementAddress.objects.create(
            tenant=request.tenant,
            account=account,
            **{key: value for key, value in data.items() if key != 'account'},
        )
        return Response(MarketplacePlacementAddressSerializer(address).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Accounts'])
class MarketplacePlacementAddressDetailView(APIView):
    """PATCH/DELETE /api/v1/accounts/placement-addresses/{id}/."""

    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    def _get_address(self, pk, tenant):
        try:
            return MarketplacePlacementAddress.objects.get(pk=pk, tenant=tenant)
        except MarketplacePlacementAddress.DoesNotExist:
            return None

    @extend_schema(
        operation_id='marketplace_placement_address_partial_update',
        request=MarketplacePlacementAddressSerializer,
        responses=MarketplacePlacementAddressSerializer,
    )
    def patch(self, request, pk):
        address = self._get_address(pk, request.tenant)
        if address is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = MarketplacePlacementAddressSerializer(address, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if 'account' in data and data['account'].tenant_id != request.tenant.id:
            return Response({'detail': 'Аккаунт Avito не найден'}, status=status.HTTP_404_NOT_FOUND)
        if data.get('is_default'):
            account = data.get('account', address.account)
            MarketplacePlacementAddress.objects.filter(
                tenant=request.tenant,
                account=account,
                is_default=True,
            ).exclude(pk=address.pk).update(is_default=False)
        for field, value in data.items():
            setattr(address, field, value)
        address.save()
        return Response(MarketplacePlacementAddressSerializer(address).data)

    @extend_schema(
        operation_id='marketplace_placement_address_delete',
        request=None,
        responses={204: None},
    )
    def delete(self, request, pk):
        address = self._get_address(pk, request.tenant)
        if address is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        address.is_active = False
        address.save(update_fields=['is_active'])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Category mappings'])
class UnmappedCategoriesView(APIView):
    @extend_schema(
        operation_id='marketplace_unmapped_category_list',
        summary='Получить категории без маппинга',
        responses=inline_serializer(
            name='UnmappedCategoryListResponse',
            fields={
                'unmapped': serializers.ListField(
                    child=serializers.CharField(), read_only=True,
                ),
                'count': serializers.IntegerField(read_only=True),
            },
        ),
    )
    def get(self, request):
        categories = CategoryMappingService.get_unmapped_categories(request.tenant)
        return Response({'unmapped': categories, 'count': len(categories)})


@extend_schema(tags=['Category mappings'])
class CategoryMappingListView(APIView):
    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(
        operation_id='marketplace_category_mapping_list',
        summary='Получить маппинги категорий',
        responses=CategoryMappingSerializer(many=True),
    )
    def get(self, request):
        qs = CategoryMapping.objects.filter(tenant=request.tenant)
        return Response(CategoryMappingSerializer(qs, many=True).data)

    @extend_schema(
        operation_id='marketplace_category_mapping_create',
        summary='Создать маппинг категории',
        request=CategoryMappingWriteSerializer,
        responses={201: CategoryMappingSerializer},
    )
    def post(self, request):
        serializer = CategoryMappingWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        mapping, _ = CategoryMapping.objects.update_or_create(
            tenant=request.tenant,
            marketplace=CategoryMapping.MARKETPLACE_AVITO,
            category_source=data['category_source'],
            defaults={
                'category_target': data['category_target'],
                'category_id': data['category_id'],
                'attributes_map': data.get('attributes_map', {}),
            },
        )
        return Response(CategoryMappingSerializer(mapping).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Category mappings'])
class CategoryMappingDetailView(APIView):
    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(
        operation_id='marketplace_category_mapping_retrieve',
        summary='Получить маппинг категории',
        responses=CategoryMappingSerializer,
    )
    def get(self, request, pk):
        mapping = CategoryMapping.objects.get(pk=pk, tenant=request.tenant)
        return Response(CategoryMappingSerializer(mapping).data)

    @extend_schema(
        operation_id='marketplace_category_mapping_update',
        summary='Обновить маппинг категории',
        request=CategoryMappingWriteSerializer,
        responses=CategoryMappingSerializer,
    )
    def put(self, request, pk):
        mapping = CategoryMapping.objects.get(pk=pk, tenant=request.tenant)
        serializer = CategoryMappingWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        for field, value in data.items():
            setattr(mapping, field, value)
        mapping.version += 1
        mapping.save()
        return Response(CategoryMappingSerializer(mapping).data)

    @extend_schema(
        operation_id='marketplace_category_mapping_delete',
        summary='Удалить маппинг категории',
        request=None,
        responses={204: None},
    )
    def delete(self, request, pk):
        CategoryMapping.objects.filter(pk=pk, tenant=request.tenant).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Listings'])
class ListingListView(APIView):
    """
    GET /api/v1/listings/ — листинги тенанта с фильтром по статусу и пагинацией.

    Query params:
        status   — draft | pending | active | rejected | archived | requires_review
        account  — id аккаунта MarketplaceAccount
    """

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_listing_list',
        parameters=[
            OpenApiParameter(
                'status', OpenApiTypes.STR, OpenApiParameter.QUERY,
                description='Фильтр по статусу листинга.',
            ),
            OpenApiParameter(
                'account', OpenApiTypes.INT, OpenApiParameter.QUERY,
                description='Фильтр по ID аккаунта маркетплейса.',
            ),
            OpenApiParameter(
                'page', OpenApiTypes.INT, OpenApiParameter.QUERY,
                description='Номер страницы.',
            ),
            OpenApiParameter(
                'page_size', OpenApiTypes.INT, OpenApiParameter.QUERY,
                description='Размер страницы, максимум 500.',
            ),
        ],
        responses=inline_serializer(
            name='MarketplaceListingListResponse',
            fields={
                'status': serializers.CharField(read_only=True),
                'data': ListingSerializer(many=True, read_only=True),
                'meta': inline_serializer(
                    name='MarketplaceListingPaginationMeta',
                    fields={
                        'total': serializers.IntegerField(read_only=True),
                        'page': serializers.IntegerField(read_only=True),
                        'page_size': serializers.IntegerField(read_only=True),
                        'next': serializers.URLField(
                            allow_null=True, read_only=True,
                        ),
                        'prev': serializers.URLField(
                            allow_null=True, read_only=True,
                        ),
                    },
                ),
            },
        ),
    )
    def get(self, request):
        """Возвращает страницу листингов текущего тенанта."""
        qs = (
            Listing.objects.filter(tenant=request.tenant, account__is_active=True)
            .exclude(status=Listing.STATUS_DELETED)
            .select_related('product', 'account')
            .order_by('-created_at')
        )

        listing_status = request.query_params.get('status', '').strip()
        if listing_status:
            qs = qs.filter(status=listing_status)

        account_id = request.query_params.get('account', '').strip()
        if account_id:
            qs = qs.filter(account_id=account_id)

        paginator = MapPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(ListingSerializer(page, many=True).data)


@extend_schema(tags=['Listings'])
class ListingDetailView(APIView):
    """
    GET  /api/v1/listings/{id}/ — детали листинга с AI-полями и фотографиями.
    PATCH /api/v1/listings/{id}/ — обновить title / description_ai.
    """

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_listing_retrieve',
        responses=_ok_response(
            'MarketplaceListingDetailResponse',
            ListingDetailSerializer(read_only=True),
        ),
    )
    def get(self, request, pk):
        """Возвращает полные данные листинга включая AI-описание и изображения товара."""
        try:
            listing = ListingService.get_for_tenant(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        return Response({'status': 'ok', 'data': ListingDetailSerializer(listing, context={'request': request}).data})

    @extend_schema(
        operation_id='marketplace_listing_partial_update',
        request=LISTING_UPDATE_REQUEST,
        responses=_ok_response(
            'MarketplaceListingUpdateResponse',
            ListingDetailSerializer(read_only=True),
        ),
    )
    def patch(self, request, pk):
        """
        Обновляет редактируемые поля листинга.

        Для active разрешено только безопасное обновление цены; остальные поля
        доступны в любом статусе кроме active и deleted.
        """
        title = request.data.get('title')
        description_ai = request.data.get('description_ai')
        placement_serializer = ListingPlacementSerializer(data=request.data, partial=True)
        placement_serializer.is_valid(raise_exception=True)
        fields_serializer = ListingFieldsSerializer(data=request.data, partial=True)
        fields_serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                current = ListingService.get_for_tenant(pk, request.tenant)
                active_price_only = (
                    current.status == Listing.STATUS_ACTIVE
                    and bool(fields_serializer.validated_data)
                    and set(request.data) <= {'price_on_listing', 'margin_pct'}
                )
                if active_price_only:
                    listing = ListingService.update_listing_fields(
                        pk, request.tenant, fields_serializer.validated_data,
                    )
                    return Response({
                        'status': 'ok',
                        'data': ListingDetailSerializer(
                            listing, context={'request': request},
                        ).data,
                    })
                listing = ListingService.update_content(pk, request.tenant, title, description_ai)
                listing = ListingService.update_listing_fields(pk, request.tenant, fields_serializer.validated_data)
                listing = ListingService.update_placement(pk, request.tenant, placement_serializer.validated_data)
        except ListingNotFound as exc:
            return Response({'status': 'error', 'code': 'not_found', 'message': str(exc)},
                            status=status.HTTP_404_NOT_FOUND)
        except ListingAccountConflict as exc:
            return Response({'status': 'error', 'code': 'account_conflict', 'message': str(exc)},
                            status=status.HTTP_409_CONFLICT)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'data': ListingDetailSerializer(listing, context={'request': request}).data})


@extend_schema(tags=['Listings'])
class ListingBulkPlacementView(APIView):
    """POST /api/v1/listings/bulk-placement/ — массово назначить адресные поля."""

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_listing_bulk_placement',
        request=ListingBulkPlacementSerializer,
        responses=_ok_response(
            'MarketplaceListingBulkPlacementResponse',
            inline_serializer(
                name='MarketplaceListingBulkPlacementResult',
                fields={
                    'updated': serializers.IntegerField(read_only=True),
                },
            ),
        ),
    )
    def post(self, request):
        serializer = ListingBulkPlacementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        filters = {
            field: data.get(field)
            for field in ('listing_ids', 'account_id', 'status', 'category_source', 'catalog_category_id')
        }
        try:
            updated = ListingService.bulk_update_placement(request.tenant, filters, data)
        except ListingBulkLimitExceeded as exc:
            return Response(
                {'status': 'error', 'code': 'bulk_limit_exceeded', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'ok', 'data': {'updated': updated}})


@extend_schema(tags=['Listings'])
class ListingBulkActionView(APIView):
    """POST /api/v1/listings/bulk-actions/ — массовые действия с листингами."""

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_listing_bulk_action',
        request=ListingBulkActionSerializer,
        responses=_ok_response(
            'MarketplaceListingBulkActionResponse',
            inline_serializer(
                name='MarketplaceListingBulkActionResult',
                fields={
                    'total': serializers.IntegerField(read_only=True),
                    'success': serializers.IntegerField(read_only=True),
                    'skipped': serializers.IntegerField(read_only=True),
                    'errors': serializers.IntegerField(read_only=True),
                    'items': inline_serializer(
                        name='MarketplaceListingBulkActionItem',
                        many=True,
                        fields={
                            'id': serializers.IntegerField(read_only=True),
                            'status': serializers.CharField(read_only=True),
                            'message': serializers.CharField(read_only=True),
                        },
                    ),
                },
            ),
        ),
    )
    def post(self, request):
        serializer = ListingBulkActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = ListingService.bulk_action(request.tenant, serializer.validated_data)
        except ListingBulkLimitExceeded as exc:
            return Response(
                {'status': 'error', 'code': 'bulk_limit_exceeded', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'ok', 'data': result})


@extend_schema(tags=['Listings'])
class ListingApproveView(APIView):
    """POST /api/v1/listings/{id}/approve/ — одобрить листинг requires_review и опубликовать."""

    api_key_scopes = {}

    @extend_schema(
        operation_id='marketplace_listing_approve',
        request=None,
        responses=_ok_response(
            'MarketplaceListingApproveResponse',
            ListingDetailSerializer(read_only=True),
        ),
    )
    def post(self, request, pk):
        """Одобряет листинг и ставит задачу публикации в Celery."""
        try:
            listing = ListingService.approve(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'data': ListingDetailSerializer(listing, context={'request': request}).data})


@extend_schema(tags=['Listings'])
class ListingRefreshBrandCatalogView(APIView):
    """POST — внепланово обновить справочник Avito и повторно проверить бренд."""

    api_key_scopes = {}

    @extend_schema(
        operation_id='marketplace_listing_refresh_brand_catalog',
        request=None,
        responses=_ok_response(
            'MarketplaceListingRefreshBrandCatalogResponse',
            ListingDetailSerializer(read_only=True),
        ),
    )
    def post(self, request, pk):
        try:
            listing = ListingService.get_for_tenant(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        try:
            from apps.marketplaces.adapters.avito.brand_sync import sync_brand_catalog
            sync_brand_catalog(listing.account)
        except Exception as exc:
            return Response({
                'status': 'error',
                'code': 'catalog_sync_failed',
                'message': f'Не удалось обновить справочник Avito: {exc}',
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        listing = ListingService.get_for_tenant(pk, request.tenant)
        return Response({
            'status': 'ok',
            'data': ListingDetailSerializer(listing, context={'request': request}).data,
        })


@extend_schema(tags=['Listings'])
class ListingPublishView(APIView):
    """POST /api/v1/listings/{id}/publish/ — опубликовать черновик/отклонённый/архивный листинг."""

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_listing_publish',
        request=None,
        responses=_ok_response(
            'MarketplaceListingPublishResponse',
            ListingDetailSerializer(read_only=True),
        ),
    )
    def post(self, request, pk):
        """Ставит задачу публикации листинга в Celery."""
        try:
            listing = ListingService.publish(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'data': ListingDetailSerializer(listing, context={'request': request}).data})


@extend_schema(tags=['Listings'])
class ListingArchiveView(APIView):
    """POST /api/v1/listings/{id}/archive/ — снять объявление с публикации."""

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_listing_archive',
        request=None,
        responses=_ok_response(
            'MarketplaceListingArchiveResponse',
            ListingDetailSerializer(read_only=True),
        ),
    )
    def post(self, request, pk):
        try:
            listing = ListingService.archive(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'data': ListingDetailSerializer(listing, context={'request': request}).data})


@extend_schema(tags=['Listings'])
class ListingDeleteView(APIView):
    """POST /api/v1/listings/{id}/delete/ — удалить объявление через feed Remove."""

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_listing_delete',
        request=None,
        responses=_ok_response(
            'MarketplaceListingDeleteResponse',
            ListingDetailSerializer(read_only=True),
        ),
    )
    def post(self, request, pk):
        try:
            listing = ListingService.delete(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'data': ListingDetailSerializer(listing, context={'request': request}).data})


@extend_schema(tags=['Listings'])
class ListingCheckStatusView(APIView):
    """POST /api/v1/listings/{id}/check-status/ — вручную проверить статус Avito feed."""

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_listing_check_status',
        request=None,
        responses=inline_serializer(
            name='MarketplaceListingCheckStatusResponse',
            fields={
                'status': serializers.CharField(read_only=True),
                'message': serializers.CharField(read_only=True),
                'data': ListingDetailSerializer(read_only=True),
            },
        ),
    )
    def post(self, request, pk):
        try:
            listing = ListingService.check_avito_status(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({
            'status': 'ok',
            'message': 'Проверка статуса Avito поставлена в очередь',
            'data': ListingDetailSerializer(listing, context={'request': request}).data,
        })


@extend_schema(tags=['Listings'])
class ListingRegenerateView(APIView):
    """POST /api/v1/listings/{id}/regenerate/ — перегенерировать AI-описание."""

    api_key_enabled = True
    api_key_scopes = {'POST': {
        'catalog:write', 'listings:write', 'ai:run',
        'research:run', 'media:write',
    }}

    @extend_schema(
        operation_id='marketplace_listing_regenerate',
        request=None,
        responses=inline_serializer(
            name='MarketplaceListingRegenerateResponse',
            fields={
                'status': serializers.CharField(read_only=True),
                'message': serializers.CharField(read_only=True),
            },
        ),
    )
    def post(self, request, pk):
        """Ставит задачу генерации AI-описания для товара в очередь Celery."""
        from apps.billing.services import LimitChecker
        can, reason = LimitChecker().can_generate_ai(request.tenant)
        if not can:
            return Response(
                {'status': 'error', 'code': 'quota_exceeded', 'message': reason},
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        try:
            ListingService.request_regenerate(pk, request.tenant)
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({'status': 'ok', 'message': 'Задача генерации поставлена в очередь'})


@extend_schema(tags=['Analytics'])
class AnalyticsView(APIView):
    """
    GET /api/v1/analytics/ — агрегированная статистика листингов тенанта.

    Query params:
        date_from — YYYY-MM-DD (по умолчанию 30 дней назад)
        date_to   — YYYY-MM-DD (по умолчанию сегодня)
    """

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_analytics_retrieve',
        parameters=[
            OpenApiParameter(
                'date_from', OpenApiTypes.DATE, OpenApiParameter.QUERY,
                description='Начало периода; по умолчанию 29 дней назад.',
            ),
            OpenApiParameter(
                'date_to', OpenApiTypes.DATE, OpenApiParameter.QUERY,
                description='Конец периода; по умолчанию сегодня.',
            ),
        ],
        responses=_ok_response(
            'MarketplaceAnalyticsResponse',
            inline_serializer(
                name='MarketplaceAnalyticsData',
                fields={
                    'summary': inline_serializer(
                        name='MarketplaceAnalyticsSummary',
                        fields={
                            'views': serializers.IntegerField(read_only=True),
                            'contacts': serializers.IntegerField(read_only=True),
                            'impressions': serializers.IntegerField(read_only=True),
                            'avg_ctr': serializers.FloatField(read_only=True),
                            'active_listings': serializers.IntegerField(
                                read_only=True,
                            ),
                        },
                    ),
                    'daily': inline_serializer(
                        name='MarketplaceAnalyticsDailyPoint',
                        many=True,
                        fields={
                            'date': serializers.DateField(read_only=True),
                            'views': serializers.IntegerField(read_only=True),
                            'contacts': serializers.IntegerField(read_only=True),
                            'impressions': serializers.IntegerField(
                                read_only=True,
                            ),
                        },
                    ),
                    'date_from': serializers.DateField(read_only=True),
                    'date_to': serializers.DateField(read_only=True),
                },
            ),
        ),
    )
    def get(self, request):
        """Возвращает сводку и помесячную/ежедневную статистику просмотров."""
        today = datetime.date.today()
        date_from_str = request.query_params.get('date_from', '')
        date_to_str = request.query_params.get('date_to', '')

        try:
            date_from = (
                datetime.date.fromisoformat(date_from_str)
                if date_from_str else today - datetime.timedelta(days=29)
            )
            date_to = (
                datetime.date.fromisoformat(date_to_str)
                if date_to_str else today
            )
        except ValueError:
            return Response(
                {'status': 'error', 'code': 'invalid_date',
                 'detail': 'Формат даты: YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = ListingStats.objects.filter(
            tenant=request.tenant,
            date__gte=date_from,
            date__lte=date_to,
        )

        totals = qs.aggregate(
            total_views=Sum('views'),
            total_contacts=Sum('contacts'),
            total_impressions=Sum('impressions'),
        )
        total_views = totals['total_views'] or 0
        total_contacts = totals['total_contacts'] or 0
        total_impressions = totals['total_impressions'] or 0
        avg_ctr = round(total_views / total_impressions * 100, 2) if total_impressions else 0.0

        # Активные листинги тенанта
        active_listings = Listing.objects.filter(
            tenant=request.tenant, status=Listing.STATUS_ACTIVE,
        ).count()

        # Дневные точки для графика
        daily = list(
            qs.values('date')
            .annotate(
                views=Sum('views'),
                contacts=Sum('contacts'),
                impressions=Sum('impressions'),
            )
            .order_by('date')
            .values('date', 'views', 'contacts', 'impressions')
        )
        for row in daily:
            row['date'] = str(row['date'])

        return Response({
            'status': 'ok',
            'data': {
                'summary': {
                    'views': total_views,
                    'contacts': total_contacts,
                    'impressions': total_impressions,
                    'avg_ctr': avg_ctr,
                    'active_listings': active_listings,
                },
                'daily': daily,
                'date_from': str(date_from),
                'date_to': str(date_to),
            },
        })
