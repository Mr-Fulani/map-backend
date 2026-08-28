import datetime
import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
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
from apps.core.idempotency import (
    IdempotencyConflict,
    canonical_payload_fingerprint,
    raise_on_fingerprint_conflict,
)
from apps.core.models import BackgroundJobDispatch, PaidIngressIntent
from apps.marketplaces.serializers import (
    CategoryMappingSerializer,
    CategoryMappingWriteSerializer,
    AutoloadOnboardingSerializer,
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
from apps.core.throttling import (
    PrincipalScopedRateThrottle,
    TenantScopedRateThrottle,
    consume_transactional_tenant_daily_budget,
)
from apps.marketplaces.services import (
    AccountAlreadyExists,
    AvitoAccountStatusService,
    CategoryMappingService,
    InvalidMarketplaceCredentials,
    ListingBulkLimitExceeded,
    ListingAccountConflict,
    InvalidListingStatus,
    ListingPublicationValidationError,
    ListingNotFound,
    ListingService,
    MarketplaceAccountFeedConflict,
    MarketplaceAccountService,
    MarketplacePlacementAddressService,
)
from apps.tenants.permissions import TenantAdminPermission, TenantAdminWritePermission


logger = logging.getLogger(__name__)


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


class ListingRegenerateRequestSerializer(serializers.Serializer):
    idempotency_key = serializers.UUIDField(required=True)

    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({
                key: ['Неизвестное поле.'] for key in sorted(unknown)
            })
        return super().to_internal_value(data)


LISTING_REGENERATE_DATA = inline_serializer(
    name='MarketplaceListingRegenerateData',
    fields={
        'intent_id': serializers.IntegerField(read_only=True),
        'dispatch_id': serializers.UUIDField(read_only=True),
        'state': serializers.CharField(read_only=True),
        'mode': serializers.CharField(read_only=True),
        'job_id': serializers.IntegerField(read_only=True, allow_null=True),
    },
)


LISTING_REGENERATE_RESPONSE = inline_serializer(
    name='MarketplaceListingRegenerateResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'message': serializers.CharField(read_only=True),
        'data': LISTING_REGENERATE_DATA,
    },
)


LISTING_IDEMPOTENCY_CONFLICT_RESPONSE = inline_serializer(
    name='MarketplaceListingIdempotencyConflictResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'code': serializers.CharField(read_only=True),
        'message': serializers.CharField(read_only=True),
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
        ).select_related('avito_status', 'feed_endpoint')
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
            payload: dict[str, object] = {
                'detail': str(exc),
                'code': 'account_exists',
            }
            existing = (
                MarketplaceAccount.objects.select_related(
                    'avito_status', 'feed_endpoint',
                ).filter(
                    pk=exc.account_id,
                    tenant=request.tenant,
                ).first()
            )
            if existing is not None:
                payload['account'] = MarketplaceAccountSerializer(existing).data
            return Response(payload, status=status.HTTP_409_CONFLICT)
        except MarketplaceAccountFeedConflict as exc:
            return Response({
                'status': 'error',
                'code': 'feed_owner_conflict',
                'message': str(exc),
            }, status=status.HTTP_409_CONFLICT)
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
                'avito_status', 'feed_endpoint',
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
        except MarketplaceAccountFeedConflict as exc:
            return Response({
                'status': 'error',
                'code': 'feed_owner_conflict',
                'message': str(exc),
            }, status=status.HTTP_409_CONFLICT)
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
        try:
            account = MarketplaceAccountService.update_partial(account, data)
        except MarketplaceAccountFeedConflict as exc:
            return Response({
                'status': 'error',
                'code': 'feed_profile_conflict',
                'message': str(exc),
            }, status=status.HTTP_409_CONFLICT)
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
        try:
            account.soft_delete()
        except MarketplaceAccountFeedConflict as exc:
            return Response({
                'status': 'error',
                'code': 'feed_profile_conflict',
                'message': str(exc),
            }, status=status.HTTP_409_CONFLICT)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Accounts'])
class AutoloadStatusView(APIView):
    """Read Avito status and explicitly retry managed endpoint onboarding."""

    permission_classes = [IsAuthenticated, TenantAdminPermission]

    @extend_schema(
        operation_id='marketplace_account_autoload_status',
        responses=inline_serializer(
            name='MarketplaceAccountAutoloadStatusResponse',
            fields={
                'activated': serializers.BooleanField(read_only=True),
                'feed_url': serializers.URLField(
                    read_only=True,
                    allow_null=True,
                ),
                'feed_endpoint_managed': serializers.BooleanField(
                    read_only=True,
                ),
                'stale': serializers.BooleanField(read_only=True),
                'status': AvitoAccountStatusSerializer(read_only=True),
                'autoload_onboarding': AutoloadOnboardingSerializer(
                    read_only=True,
                ),
                'activate_url': serializers.URLField(
                    required=False,
                ),
            },
        ),
    )
    def get(self, request, pk):
        """
        Проверяет активирован ли Avito Autoload для аккаунта.

        Legacy-аккаунт получает прежний публичный URL. Для managed endpoint
        capability URL не раскрывается: ``feed_url`` равен ``null``.
        """
        try:
            account = MarketplaceAccount.objects.select_related(
                'avito_status', 'feed_endpoint',
            ).get(pk=pk, tenant=request.tenant)
        except MarketplaceAccount.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
        from apps.marketplaces.autoload_onboarding import (
            autoload_onboarding_presentation,
        )
        from apps.marketplaces.feed_cutover import private_feed_fleet_enabled
        from apps.marketplaces.feed_profile_migration import (
            ensure_fleet_feed_endpoint,
        )
        from apps.marketplaces.models import MarketplaceFeedEndpoint

        if private_feed_fleet_enabled():
            try:
                ensure_fleet_feed_endpoint(account)
            except Exception:
                from apps.marketplaces.autoload_onboarding import (
                    EXHAUSTED,
                    record_autoload_onboarding_state,
                )
                record_autoload_onboarding_state(
                    account,
                    code=EXHAUSTED,
                    message=(
                        'MAP не смог безопасно подготовить feed endpoint. '
                        'Повторите после проверки конфигурации.'
                    ),
                )

        status_obj = AvitoAccountStatusService.refresh(account)
        account = MarketplaceAccount.objects.select_related(
            'avito_status', 'feed_endpoint',
        ).get(pk=account.pk)
        snapshot = AvitoAccountStatusSerializer(status_obj).data
        activated = status_obj.autoload_status == status_obj.AUTOLOAD_ENABLED
        feed_endpoint_managed = MarketplaceFeedEndpoint.objects.filter(
            account_id=account.pk,
        ).exists()
        payload = {
            'activated': activated,
            'feed_url': (
                None
                if feed_endpoint_managed
                else AvitoAdapter(account)._feed_public_url()
            ),
            'feed_endpoint_managed': feed_endpoint_managed,
            'stale': snapshot['profile_stale'],
            'status': snapshot,
            'autoload_onboarding': AutoloadOnboardingSerializer(
                autoload_onboarding_presentation(account),
            ).data,
        }
        if not activated:
            payload['activate_url'] = 'https://www.avito.ru/autoload/settings'
        return Response(payload)

    @extend_schema(
        operation_id='marketplace_account_autoload_retry',
        request=None,
        responses={
            200: AutoloadOnboardingSerializer,
            202: AutoloadOnboardingSerializer,
            409: AutoloadOnboardingSerializer,
        },
    )
    def post(self, request, pk):
        """Explicitly retry only a replay-safe Autoload onboarding state."""

        try:
            account = MarketplaceAccount.objects.select_related(
                'avito_status', 'feed_endpoint',
            ).get(pk=pk, tenant=request.tenant)
        except MarketplaceAccount.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)

        from apps.marketplaces.autoload_onboarding import (
            autoload_onboarding_presentation,
            clear_autoload_onboarding_state,
            schedule_autoload_profile_setup,
        )
        from apps.marketplaces.feed_cutover import private_feed_fleet_enabled
        from apps.marketplaces.feed_profile_migration import (
            ensure_fleet_feed_endpoint,
        )

        if private_feed_fleet_enabled():
            try:
                ensure_fleet_feed_endpoint(account)
            except Exception:
                from apps.marketplaces.autoload_onboarding import (
                    EXHAUSTED,
                    record_autoload_onboarding_state,
                )
                record_autoload_onboarding_state(
                    account,
                    code=EXHAUSTED,
                    message=(
                        'MAP не смог безопасно подготовить feed endpoint. '
                        'Повторите после проверки конфигурации.'
                    ),
                )
                account = MarketplaceAccount.objects.select_related(
                    'avito_status', 'feed_endpoint',
                ).get(pk=account.pk)
                presentation = autoload_onboarding_presentation(account)
                return Response(
                    AutoloadOnboardingSerializer(presentation).data,
                    status=status.HTTP_409_CONFLICT,
                )
        account = MarketplaceAccount.objects.select_related(
            'avito_status', 'feed_endpoint',
        ).get(pk=account.pk)
        presentation = autoload_onboarding_presentation(account)
        if presentation.ready:
            return Response(AutoloadOnboardingSerializer(presentation).data)
        if presentation.state == 'manual_review':
            return Response(
                AutoloadOnboardingSerializer(presentation).data,
                status=status.HTTP_409_CONFLICT,
            )

        clear_autoload_onboarding_state(account)
        schedule_autoload_profile_setup(account.pk, account.tenant_id)
        account = MarketplaceAccount.objects.select_related(
            'avito_status', 'feed_endpoint',
        ).get(pk=account.pk)
        return Response(
            AutoloadOnboardingSerializer(
                autoload_onboarding_presentation(account),
            ).data,
            status=status.HTTP_202_ACCEPTED,
        )


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
        data['account'] = account
        address = MarketplacePlacementAddressService.create(
            request.tenant,
            data,
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
        try:
            address = MarketplacePlacementAddressService.update(
                address,
                request.tenant,
                data,
            )
        except MarketplacePlacementAddress.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
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
        try:
            MarketplacePlacementAddressService.deactivate(
                address,
                request.tenant,
            )
        except MarketplacePlacementAddress.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
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
        mapping = CategoryMappingService.upsert(request.tenant, data)
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
        mapping = CategoryMappingService.update(mapping, data)
        return Response(CategoryMappingSerializer(mapping).data)

    @extend_schema(
        operation_id='marketplace_category_mapping_delete',
        summary='Удалить маппинг категории',
        request=None,
        responses={204: None},
    )
    def delete(self, request, pk):
        CategoryMappingService.delete(request.tenant, pk)
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
            .select_related('product', 'account', 'feed_run')
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
        except ListingPublicationValidationError as exc:
            return Response({
                'status': 'error',
                'code': 'listing_validation_error',
                'message': str(exc),
                'field_errors': exc.field_errors,
            }, status=status.HTTP_400_BAD_REQUEST)
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
            # Provider exceptions may embed request URLs or other integration
            # details. Keep the client response and logs structural.
            logger.warning(
                'Avito brand catalog refresh failed for listing=%s (%s).',
                listing.pk,
                type(exc).__name__,
            )
            return Response({
                'status': 'error',
                'code': 'catalog_sync_failed',
                'message': 'Не удалось обновить справочник Avito.',
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        listing = ListingService.get_for_tenant(pk, request.tenant)
        return Response({
            'status': 'ok',
            'data': ListingDetailSerializer(listing, context={'request': request}).data,
        })


@extend_schema(tags=['Listings'])
class ListingPublishView(APIView):
    """POST /api/v1/listings/{id}/publish/ — публикация или безопасный retry до отправки."""

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
        except ListingPublicationValidationError as exc:
            return Response({
                'status': 'error',
                'code': 'listing_validation_error',
                'message': str(exc),
                'field_errors': exc.field_errors,
            }, status=status.HTTP_400_BAD_REQUEST)
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
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'expensive_research_principal'
    tenant_throttle_scope = 'expensive_research_tenant'
    expensive_throttle_methods = {'POST'}

    @extend_schema(
        operation_id='marketplace_listing_regenerate',
        request=ListingRegenerateRequestSerializer,
        responses={
            202: LISTING_REGENERATE_RESPONSE,
            409: LISTING_IDEMPOTENCY_CONFLICT_RESPONSE,
        },
    )
    def post(self, request, pk):
        """Ставит задачу генерации AI-описания для товара в очередь Celery."""
        from apps.products.services import QuotaExceeded

        request_serializer = ListingRegenerateRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        idempotency_key = request_serializer.validated_data['idempotency_key']
        canonical_request = {
            'listing_id': int(pk),
            'payload': {},
        }
        fingerprint = canonical_payload_fingerprint(canonical_request)
        raw_request = {
            str(key): value
            for key, value in request.data.items()
            if key != 'idempotency_key'
        }
        raw_fingerprint = canonical_payload_fingerprint(raw_request)
        try:
            with transaction.atomic():
                type(request.tenant).objects.select_for_update().only('pk').get(
                    pk=request.tenant.pk,
                )
                intent = PaidIngressIntent.objects.select_related('dispatch').filter(
                    tenant=request.tenant,
                    operation='listing-regenerate',
                    idempotency_key=idempotency_key,
                ).first()
                if intent is not None:
                    raise_on_fingerprint_conflict(
                        intent.request_fingerprint,
                        fingerprint,
                    )
                    raise_on_fingerprint_conflict(
                        intent.raw_payload_fingerprint,
                        raw_fingerprint,
                    )
                    if (
                        intent.resource_type != 'marketplaces.listing'
                        or intent.resource_id != str(pk)
                        or intent.dispatch is None
                    ):
                        return Response(
                            {
                                'status': 'error',
                                'code': 'idempotency_incomplete',
                                'message': 'Исходный результат запроса недоступен.',
                            },
                            status=status.HTTP_409_CONFLICT,
                        )
                    dispatch = intent.dispatch
                    submission = intent.result_metadata
                else:
                    # Lock the canonical resource as well as the tenant so a
                    # concurrent status mutation cannot race the validation.
                    Listing.objects.select_for_update().filter(
                        pk=pk,
                        tenant=request.tenant,
                    ).exists()
                    deduplication_key = (
                        f'listing-regenerate:{request.tenant.pk}:'
                        f'{idempotency_key}:{fingerprint}'
                    )
                    listing = ListingService.request_regenerate(
                        pk,
                        request.tenant,
                        durable_deduplication_key=deduplication_key,
                    )
                    submission = listing.__dict__['_regeneration_submission']
                    dispatch = BackgroundJobDispatch.objects.get(
                        pk=submission['dispatch_id'],
                        deduplication_key=deduplication_key,
                    )
                    if submission.get('mode') == 'enrich_then_generate':
                        consume_transactional_tenant_daily_budget(
                            tenant=request.tenant,
                            scope='product-parse-jobs',
                            cost=1,
                            limit=settings.PRODUCT_PARSE_TENANT_DAILY_JOBS,
                        )
                    intent = PaidIngressIntent.objects.create(
                        tenant=request.tenant,
                        operation='listing-regenerate',
                        idempotency_key=idempotency_key,
                        request_fingerprint=fingerprint,
                        raw_payload_fingerprint=raw_fingerprint,
                        request_payload={
                            'raw': raw_request,
                            'canonical': canonical_request,
                        },
                        resource_type='marketplaces.listing',
                        resource_id=str(pk),
                        result_type='core.background_job_dispatch',
                        result_id=str(dispatch.pk),
                        result_metadata=submission,
                        dispatch=dispatch,
                    )
        except IdempotencyConflict as exc:
            return Response(
                {
                    'status': 'error',
                    'code': 'idempotency_conflict',
                    'message': str(exc),
                },
                status=status.HTTP_409_CONFLICT,
            )
        except ListingNotFound:
            return Response({'status': 'error', 'code': 'not_found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidListingStatus as exc:
            return Response({'status': 'error', 'code': 'invalid_status', 'message': str(exc)},
                            status=status.HTTP_400_BAD_REQUEST)
        except QuotaExceeded as exc:
            return Response(
                {
                    'status': 'error',
                    'code': 'quota_exceeded',
                    'message': str(exc),
                },
                status=status.HTTP_402_PAYMENT_REQUIRED,
            )
        return Response({
            'status': 'ok',
            'message': 'Задача генерации поставлена в очередь',
            'data': {
                'intent_id': intent.pk,
                'dispatch_id': str(dispatch.pk),
                'state': dispatch.status,
                'mode': submission['mode'],
                'job_id': submission['job_id'],
            },
        }, status=status.HTTP_202_ACCEPTED)


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
        today = timezone.localdate()
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

        if date_from > date_to:
            return Response(
                {
                    'status': 'error',
                    'code': 'invalid_date_range',
                    'detail': 'Дата начала не может быть позже даты окончания',
                },
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
