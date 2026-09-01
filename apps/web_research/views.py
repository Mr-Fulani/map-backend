from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from drf_spectacular.utils import (
    OpenApiParameter, OpenApiTypes, extend_schema, inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from apps.tenants.api_views import ResearchAPIView as APIView

from apps.core.pagination import MapPagination
from apps.core.idempotency import (
    IdempotencyConflict,
    canonical_payload_fingerprint,
    raise_on_fingerprint_conflict,
)
from apps.core.models import PaidIngressIntent
from apps.core.throttling import (
    PrincipalScopedRateThrottle,
    TenantScopedRateThrottle,
)
from apps.products.models import Product
from apps.marketplaces.models import Listing
from apps.tenants.models import TenantUser
from apps.products.services import (
    AutoPartsEnrichmentDisabled, ProductEnrichmentService, ProductIsNotAutoPart,
)
from apps.web_research.market import listing_market_comparison, product_market_comparison
from apps.web_research.models import CompetitorOffer, WebResearchRun
from apps.web_research.serializers import (
    CompetitorOfferSerializer, ListingMarketComparisonSerializer,
    TenantWebResearchSettingsSerializer, WebResearchRunListSerializer,
    WebResearchRunSerializer,
)
from apps.web_research.search_context import get_tenant_research_settings
from apps.web_research.services import (
    WebResearchReconciliationRequired,
    WebResearchService,
    enrichment_coverage,
)
from apps.web_research.providers.registry import registered_search_providers
from apps.web_research.routing import search_provider_candidates


class _StrictRequestSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({
                key: ['Неизвестное поле.'] for key in sorted(unknown)
            })
        return super().to_internal_value(data)


class WebResearchStartRequestSerializer(_StrictRequestSerializer):
    idempotency_key = serializers.UUIDField(required=True)
    search_provider = serializers.SlugField(
        required=False, allow_blank=True, max_length=50,
    )
    generate_after = serializers.BooleanField(required=False, default=False)


class MarketResearchStartRequestSerializer(_StrictRequestSerializer):
    idempotency_key = serializers.UUIDField(required=True)
    search_provider = serializers.SlugField(
        required=False, allow_blank=True, max_length=50,
    )
    force = serializers.BooleanField(required=False, default=False)


class ProductMarketComparisonQuerySerializer(_StrictRequestSerializer):
    reference_price = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal('0.01'),
        required=False,
    )


_COVERAGE_SCHEMA = inline_serializer(
    name='WebResearchCoverage',
    fields={
        'score': serializers.FloatField(),
        'threshold': serializers.FloatField(),
        'missing': serializers.ListField(child=serializers.CharField()),
        'trusted_fitments': serializers.IntegerField(),
        'trusted_facts': serializers.IntegerField(),
    },
)
_RUN_RESPONSE = inline_serializer(
    name='WebResearchRunResponse',
    fields={
        'status': serializers.CharField(),
        'data': WebResearchRunSerializer(),
    },
)
_PRODUCT_RESEARCH_RESPONSE = inline_serializer(
    name='ProductWebResearchResponse',
    fields={
        'status': serializers.CharField(),
        'data': WebResearchRunSerializer(allow_null=True),
        'coverage': _COVERAGE_SCHEMA,
    },
)
_PAGINATION_META = inline_serializer(
    name='WebResearchPaginationMeta',
    fields={
        'total': serializers.IntegerField(),
        'page': serializers.IntegerField(),
        'page_size': serializers.IntegerField(),
        'next': serializers.URLField(allow_null=True),
        'prev': serializers.URLField(allow_null=True),
    },
)
_RUN_SUMMARY = inline_serializer(
    name='WebResearchRunSummary',
    fields={
        'total': serializers.IntegerField(),
        'active': serializers.IntegerField(),
        'need_review': serializers.IntegerField(),
        'failed': serializers.IntegerField(),
    },
)
_RUN_LIST_RESPONSE = inline_serializer(
    name='WebResearchRunListResponse',
    fields={
        'status': serializers.CharField(),
        'data': WebResearchRunListSerializer(many=True),
        'meta': _PAGINATION_META,
        'summary': _RUN_SUMMARY,
    },
)
_PROVIDER_RESPONSE = inline_serializer(
    name='WebSearchProviderListResponse',
    fields={
        'status': serializers.CharField(),
        'data': inline_serializer(
            name='WebSearchProviderListData',
            fields={
                'mode': serializers.CharField(),
                'available': serializers.BooleanField(),
                'providers': inline_serializer(
                    name='WebSearchProvider',
                    fields={
                        'provider_id': serializers.CharField(),
                        'display_name': serializers.CharField(),
                        'available': serializers.BooleanField(),
                    },
                    many=True,
                ),
            },
        ),
    },
)
_SETTINGS_RESPONSE = inline_serializer(
    name='TenantWebResearchSettingsResponse',
    fields={
        'status': serializers.CharField(),
        'data': TenantWebResearchSettingsSerializer(),
        'can_edit': serializers.BooleanField(),
    },
)
_MARKET_RESEARCH_RESPONSE = inline_serializer(
    name='ProductMarketResearchResponse',
    fields={
        'status': serializers.CharField(),
        'reused': serializers.BooleanField(),
        'message': serializers.CharField(required=False),
        'data': WebResearchRunSerializer(allow_null=True),
    },
)
_OFFER_LIST_RESPONSE = inline_serializer(
    name='ProductMarketOfferListResponse',
    fields={
        'status': serializers.CharField(),
        'data': CompetitorOfferSerializer(many=True),
        'meta': _PAGINATION_META,
    },
)
_MARKET_COMPARISON_RESPONSE = inline_serializer(
    name='ListingMarketComparisonResponse',
    fields={
        'status': serializers.CharField(),
        'data': ListingMarketComparisonSerializer(),
    },
)

_IDEMPOTENCY_ERROR_RESPONSE = inline_serializer(
    name='WebResearchIdempotencyErrorResponse',
    fields={
        'status': serializers.CharField(),
        'code': serializers.CharField(),
        'message': serializers.CharField(),
    },
)


def _idempotency_error(code: str, message: str) -> Response:
    return Response(
        {'status': 'error', 'code': code, 'message': message},
        status=status.HTTP_409_CONFLICT,
    )


def _intent_run_or_error(
    intent: PaidIngressIntent,
    *,
    product_id: int,
    purposes: list[str],
) -> tuple[WebResearchRun | None, Response | None]:
    metadata = intent.result_metadata
    if not isinstance(metadata, dict) or not metadata.get('has_run', False):
        return None, None
    try:
        run_id = int(intent.result_id)
    except (TypeError, ValueError):
        return None, _idempotency_error(
            'idempotency_incomplete',
            'Исходный результат запроса недоступен.',
        )
    run = WebResearchRun.objects.filter(
        pk=run_id,
        tenant=intent.tenant,
        product_id=product_id,
        purpose__in=purposes,
    ).first()
    if run is None:
        return None, _idempotency_error(
            'idempotency_incomplete',
            'Исходный результат запроса недоступен.',
        )
    return run, None


@extend_schema(tags=['Web research'])
class ProductWebResearchView(APIView):
    """POST starts a manual grounded web research run for one tenant product."""

    api_key_enabled = True
    api_key_scopes = {
        'GET': {'research:read'},
        'HEAD': {'research:read'},
        'OPTIONS': {'research:read'},
        'POST': {'research:run', 'catalog:write', 'ai:run'},
    }
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'expensive_research_principal'
    tenant_throttle_scope = 'expensive_research_tenant'
    expensive_throttle_methods = {'POST'}

    @extend_schema(
        request=WebResearchStartRequestSerializer,
        responses={
            200: _RUN_RESPONSE,
            201: _RUN_RESPONSE,
            409: _IDEMPOTENCY_ERROR_RESPONSE,
        },
    )
    def post(self, request, product_pk: int):
        request_serializer = WebResearchStartRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        generate_after = request_serializer.validated_data['generate_after']
        generate_scopes = {'listings:write'}
        if (
            generate_after
            and getattr(request.user, 'is_api_key', False)
            and not request.user.has_scopes(generate_scopes)
        ):
            raise PermissionDenied(
                'API Key требует scope listings:write '
                'для последующей генерации.',
            )
        idempotency_key = request_serializer.validated_data['idempotency_key']
        provider = request_serializer.validated_data.get('search_provider', '')
        canonical_request = {
            'generate_after': generate_after,
            'product_id': product_pk,
            'search_provider': provider,
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
                intent = PaidIngressIntent.objects.filter(
                    tenant=request.tenant,
                    operation='product-web-research',
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
                        intent.resource_type != 'products.product'
                        or intent.resource_id != str(product_pk)
                    ):
                        raise IdempotencyConflict(
                            'Ключ идемпотентности уже использован для другого запроса.'
                        )
                    run, incomplete = _intent_run_or_error(
                        intent,
                        product_id=product_pk,
                        purposes=[WebResearchRun.Purpose.ENRICHMENT],
                    )
                    if incomplete is not None:
                        return incomplete
                    if run is None:
                        return _idempotency_error(
                            'idempotency_incomplete',
                            'Исходный результат запроса недоступен.',
                        )
                    created = bool(intent.result_metadata.get('created', False))
                else:
                    product = get_object_or_404(
                        Product.objects.select_for_update().select_related('tenant'),
                        pk=product_pk,
                        tenant=request.tenant,
                    )
                    ProductEnrichmentService.ensure_product_auto_parts_eligible(
                        request.tenant,
                        product,
                    )
                    run, created = WebResearchService.create_run(
                        product,
                        trigger=WebResearchRun.Trigger.MANUAL,
                        generate_after=generate_after,
                        search_provider=provider,
                        purpose=WebResearchRun.Purpose.ENRICHMENT,
                    )
                    from apps.web_research.tasks import enqueue_web_research_run
                    dispatch = enqueue_web_research_run(run.pk)
                    intent = PaidIngressIntent.objects.create(
                        tenant=request.tenant,
                        operation='product-web-research',
                        idempotency_key=idempotency_key,
                        request_fingerprint=fingerprint,
                        raw_payload_fingerprint=raw_fingerprint,
                        request_payload={
                            'raw': raw_request,
                            'canonical': canonical_request,
                        },
                        resource_type='products.product',
                        resource_id=str(product.pk),
                        result_type='web_research.web_research_run',
                        result_id=str(run.pk),
                        result_metadata={
                            'created': created,
                            'has_run': True,
                        },
                        dispatch=dispatch,
                    )
        except (AutoPartsEnrichmentDisabled, ProductIsNotAutoPart) as exc:
            return Response(
                {'status': 'error', 'code': 'web_research_unavailable', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except IdempotencyConflict as exc:
            return _idempotency_error('idempotency_conflict', str(exc))
        except WebResearchReconciliationRequired as exc:
            return _idempotency_error('provider_reconciliation_required', str(exc))
        return Response({
            'status': 'ok',
            'data': WebResearchRunSerializer(run).data,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @extend_schema(responses=_PRODUCT_RESEARCH_RESPONSE)
    def get(self, request, product_pk: int):
        product = get_object_or_404(Product, pk=product_pk, tenant=request.tenant)
        run = product.web_research_runs.filter(
            purpose__in=[WebResearchRun.Purpose.ENRICHMENT, WebResearchRun.Purpose.COMBINED],
        ).order_by('-created_at').first()
        return Response({
            'status': 'ok',
            'data': WebResearchRunSerializer(run).data if run else None,
            'coverage': enrichment_coverage(product),
        })


@extend_schema(tags=['Web research'])
class WebResearchRunDetailView(APIView):
    api_key_enabled = True

    @extend_schema(
        summary='Детали запуска интернет-исследования',
        operation_id='web_research_run_retrieve',
        responses=_RUN_RESPONSE,
    )
    def get(self, request, pk: int):
        run = get_object_or_404(
            WebResearchRun.objects.prefetch_related('evidence', 'claims__evidence'),
            pk=pk,
            tenant=request.tenant,
        )
        return Response({'status': 'ok', 'data': WebResearchRunSerializer(run).data})


@extend_schema(tags=['Web research'])
class WebResearchRunListView(APIView):
    """Tenant-scoped research journal for the customer dashboard."""

    api_key_enabled = True

    @extend_schema(
        operation_id='web_research_run_list',
        parameters=[
            OpenApiParameter(
                name='status',
                type=str,
                location=OpenApiParameter.QUERY,
                enum=['active', *WebResearchRun.Status.values],
                description='Фильтр по состоянию запуска.',
            ),
            OpenApiParameter(
                name='purpose',
                type=str,
                location=OpenApiParameter.QUERY,
                enum=WebResearchRun.Purpose.values,
                description='Фильтр по назначению исследования.',
            ),
            OpenApiParameter(
                name='page',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description='Номер страницы.',
            ),
            OpenApiParameter(
                name='page_size',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description='Размер страницы (не более 500).',
            ),
        ],
        responses=_RUN_LIST_RESPONSE,
    )
    def get(self, request):
        base_queryset = WebResearchRun.objects.filter(tenant=request.tenant)
        summary = base_queryset.aggregate(
            total=Count('id'),
            active=Count('id', filter=Q(status__in=[
                WebResearchRun.Status.QUEUED,
                WebResearchRun.Status.RUNNING,
            ])),
            need_review=Count(
                'id', filter=Q(status=WebResearchRun.Status.NEED_REVIEW),
            ),
            failed=Count('id', filter=Q(status=WebResearchRun.Status.FAILED)),
        )
        queryset = base_queryset.select_related('product').order_by('-created_at')
        run_status = str(request.query_params.get('status') or '').strip()
        purpose = str(request.query_params.get('purpose') or '').strip()
        if purpose in WebResearchRun.Purpose.values:
            queryset = queryset.filter(purpose=purpose)
        if run_status == 'active':
            queryset = queryset.filter(status__in=[
                WebResearchRun.Status.QUEUED,
                WebResearchRun.Status.RUNNING,
            ])
        elif run_status in WebResearchRun.Status.values:
            queryset = queryset.filter(status=run_status)

        paginator = MapPagination()
        page = paginator.paginate_queryset(queryset, request)
        response = paginator.get_paginated_response(
            WebResearchRunListSerializer(page, many=True).data,
        )
        response.data['summary'] = summary
        return response


@extend_schema(tags=['Web research'])
class WebSearchProviderListView(APIView):
    """Tenant-safe provider health; credentials and platform policy are never exposed."""

    api_key_enabled = True

    @extend_schema(responses=_PROVIDER_RESPONSE)
    def get(self, request):
        candidates = search_provider_candidates(request.tenant)
        available = {item.provider.provider_id for item in candidates}
        providers = []
        for provider_id, provider_class in registered_search_providers().items():
            if provider_id not in available:
                continue
            providers.append({
                'provider_id': provider_id,
                'display_name': provider_class.display_name or provider_id,
                'available': True,
            })
        return Response({
            'status': 'ok',
            'data': {
                'mode': 'automatic',
                'available': bool(providers),
                'providers': providers,
            },
        })


@extend_schema(tags=['Web research'])
class TenantWebResearchSettingsView(APIView):
    """Tenant-visible search policy; only owner/admin may change it."""

    api_key_scopes = {}

    @extend_schema(responses=_SETTINGS_RESPONSE)
    def get(self, request):
        settings = get_tenant_research_settings(request.tenant)
        return Response({
            'status': 'ok',
            'data': TenantWebResearchSettingsSerializer(settings).data,
            'can_edit': self._can_edit(request),
        })

    @extend_schema(
        request=TenantWebResearchSettingsSerializer,
        responses=_SETTINGS_RESPONSE,
    )
    def put(self, request):
        if not self._can_edit(request):
            return Response(
                {'detail': 'Изменять настройки могут только владелец и администратор.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        settings = get_tenant_research_settings(request.tenant)
        serializer = TenantWebResearchSettingsSerializer(
            settings, data=request.data, partial=True,
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({'status': 'ok', 'data': serializer.data, 'can_edit': True})

    @staticmethod
    def _can_edit(request) -> bool:
        return request.tenant.members.filter(
            user=request.user,
            role__in=[TenantUser.ROLE_OWNER, TenantUser.ROLE_ADMIN],
        ).exists()


@extend_schema(tags=['Web research'])
class ProductMarketResearchView(APIView):
    """Start pricing research without coupling it to enrichment coverage."""

    api_key_enabled = True
    throttle_classes = [PrincipalScopedRateThrottle, TenantScopedRateThrottle]
    principal_throttle_scope = 'expensive_research_principal'
    tenant_throttle_scope = 'expensive_research_tenant'
    expensive_throttle_methods = {'POST'}

    @extend_schema(
        request=MarketResearchStartRequestSerializer,
        responses={
            200: _MARKET_RESEARCH_RESPONSE,
            201: _MARKET_RESEARCH_RESPONSE,
            409: _IDEMPOTENCY_ERROR_RESPONSE,
        },
    )
    def post(self, request, product_pk: int):
        request_serializer = MarketResearchStartRequestSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        idempotency_key = request_serializer.validated_data['idempotency_key']
        force = request_serializer.validated_data['force']
        provider = request_serializer.validated_data.get('search_provider', '')
        canonical_request = {
            'force': force,
            'product_id': product_pk,
            'search_provider': provider,
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
                intent = PaidIngressIntent.objects.filter(
                    tenant=request.tenant,
                    operation='product-market-research',
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
                        intent.resource_type != 'products.product'
                        or intent.resource_id != str(product_pk)
                    ):
                        raise IdempotencyConflict(
                            'Ключ идемпотентности уже использован для другого запроса.'
                        )
                    run, incomplete = _intent_run_or_error(
                        intent,
                        product_id=product_pk,
                        purposes=[
                            WebResearchRun.Purpose.PRICING,
                            WebResearchRun.Purpose.COMBINED,
                        ],
                    )
                    if incomplete is not None:
                        return incomplete
                    metadata = intent.result_metadata
                    reused = bool(metadata.get('reused', False))
                    created = bool(metadata.get('created', False))
                    message = str(metadata.get('message', ''))
                else:
                    product = get_object_or_404(
                        Product.objects.select_for_update().select_related('tenant'),
                        pk=product_pk,
                        tenant=request.tenant,
                    )
                    research_settings = get_tenant_research_settings(request.tenant)
                    if not research_settings.market_research_enabled:
                        return Response(
                            {
                                'status': 'error',
                                'code': 'market_research_disabled',
                                'message': (
                                    'Исследование цен отключено в настройках '
                                    'организации.'
                                ),
                            },
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    fresh_verified = CompetitorOffer.objects.filter(
                        tenant=request.tenant,
                        product=product,
                        expires_at__gt=now(),
                        review_status=CompetitorOffer.ReviewStatus.VERIFIED,
                    ).count()
                    message = ''
                    dispatch = None
                    if (
                        not force
                        and fresh_verified >= min(3, research_settings.result_limit)
                    ):
                        run = product.web_research_runs.filter(
                            purpose__in=[
                                WebResearchRun.Purpose.PRICING,
                                WebResearchRun.Purpose.COMBINED,
                            ],
                        ).order_by('-created_at').first()
                        created = False
                        reused = True
                        message = (
                            'Использованы свежие предложения; '
                            'платный поиск не запускался.'
                        )
                    else:
                        run, created = WebResearchService.create_run(
                            product,
                            trigger=WebResearchRun.Trigger.MANUAL,
                            search_provider=provider,
                            purpose=WebResearchRun.Purpose.PRICING,
                        )
                        reused = not created
                        from apps.web_research.tasks import enqueue_web_research_run
                        dispatch = enqueue_web_research_run(run.pk)
                    intent = PaidIngressIntent.objects.create(
                        tenant=request.tenant,
                        operation='product-market-research',
                        idempotency_key=idempotency_key,
                        request_fingerprint=fingerprint,
                        raw_payload_fingerprint=raw_fingerprint,
                        request_payload={
                            'raw': raw_request,
                            'canonical': canonical_request,
                        },
                        resource_type='products.product',
                        resource_id=str(product.pk),
                        result_type=(
                            'web_research.web_research_run'
                            if run is not None else 'fresh_verified_offers'
                        ),
                        result_id=str(run.pk) if run is not None else '',
                        result_metadata={
                            'created': created,
                            'has_run': run is not None,
                            'message': message,
                            'reused': reused,
                        },
                        dispatch=dispatch,
                    )
        except IdempotencyConflict as exc:
            return _idempotency_error('idempotency_conflict', str(exc))
        except WebResearchReconciliationRequired as exc:
            return _idempotency_error('provider_reconciliation_required', str(exc))
        response_payload = {
            'status': 'ok',
            'reused': reused,
            'data': WebResearchRunSerializer(run).data if run is not None else None,
        }
        if message:
            response_payload['message'] = message
        return Response(
            response_payload,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


@extend_schema(tags=['Web research'])
class ProductMarketOfferListView(APIView):
    api_key_enabled = True

    @extend_schema(
        summary='Рыночные предложения для товара',
        parameters=[
            OpenApiParameter(
                name='fresh', type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY, default=True,
            ),
            OpenApiParameter(
                name='country', type=str,
                location=OpenApiParameter.QUERY,
                description='Двухбуквенный код страны.',
            ),
            OpenApiParameter(
                name='in_stock', type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name='new', type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name='include_analogues', type=OpenApiTypes.BOOL,
                location=OpenApiParameter.QUERY,
            ),
            OpenApiParameter(
                name='ordering', type=str,
                location=OpenApiParameter.QUERY,
                enum=['price', '-price', 'availability', 'confidence', '-captured_at'],
            ),
            OpenApiParameter(
                name='page',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description='Номер страницы.',
            ),
            OpenApiParameter(
                name='page_size',
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description='Размер страницы (не более 500).',
            ),
        ],
        responses=_OFFER_LIST_RESPONSE,
    )
    def get(self, request, product_pk: int):
        product = get_object_or_404(Product, pk=product_pk, tenant=request.tenant)
        queryset = CompetitorOffer.objects.filter(
            tenant=request.tenant, product=product,
        ).select_related('evidence', 'run')
        if request.query_params.get('fresh', 'true').lower() != 'false':
            queryset = queryset.filter(expires_at__gt=now())
        country = str(request.query_params.get('country') or '').strip().upper()
        if country:
            queryset = queryset.filter(country_code=country)
        if request.query_params.get('in_stock') == 'true':
            queryset = queryset.filter(availability=CompetitorOffer.Availability.IN_STOCK)
        if request.query_params.get('new') == 'true':
            queryset = queryset.filter(condition=CompetitorOffer.Condition.NEW)
        if request.query_params.get('include_analogues') != 'true':
            queryset = queryset.exclude(match_type=CompetitorOffer.MatchType.ANALOGUE)
        ordering = str(request.query_params.get('ordering') or 'price')
        ordering_map = {
            'price': ('normalized_price', '-captured_at'),
            '-price': ('-normalized_price', '-captured_at'),
            'availability': ('availability', 'normalized_price'),
            'confidence': ('-match_confidence', 'normalized_price'),
            '-captured_at': ('-captured_at',),
        }
        queryset = queryset.order_by(*ordering_map.get(ordering, ordering_map['price']))
        paginator = MapPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(CompetitorOfferSerializer(page, many=True).data)


@extend_schema(tags=['Web research'])
class ListingMarketComparisonView(APIView):
    api_key_enabled = True

    @extend_schema(
        summary='Сравнение цены листинга с рынком',
        responses=_MARKET_COMPARISON_RESPONSE,
    )
    def get(self, request, listing_pk: int):
        listing = get_object_or_404(
            Listing.objects.select_related('tenant', 'product'),
            pk=listing_pk, tenant=request.tenant,
        )
        return Response({'status': 'ok', 'data': listing_market_comparison(listing)})


@extend_schema(tags=['Web research'])
class ProductMarketComparisonView(APIView):
    api_key_enabled = True

    @extend_schema(
        summary='Общее сравнение товара с рынком для выбранного канала',
        parameters=[OpenApiParameter(
            'reference_price',
            OpenApiTypes.DECIMAL,
            description='Текущая цена выбранного канала публикации.',
        )],
        responses=_MARKET_COMPARISON_RESPONSE,
    )
    def get(self, request, product_pk: int):
        query = ProductMarketComparisonQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        product = get_object_or_404(
            Product.objects.select_related('tenant'),
            pk=product_pk,
            tenant=request.tenant,
        )
        return Response({
            'status': 'ok',
            'data': product_market_comparison(
                product,
                reference_price=query.validated_data.get('reference_price'),
            ),
        })
