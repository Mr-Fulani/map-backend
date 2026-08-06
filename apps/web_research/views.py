from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils.timezone import now
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pagination import MapPagination
from apps.products.models import Product
from apps.marketplaces.models import Listing
from apps.tenants.models import TenantUser
from apps.products.services import (
    AutoPartsEnrichmentDisabled, ProductEnrichmentService, ProductIsNotAutoPart,
)
from apps.web_research.market import listing_market_comparison
from apps.web_research.models import CompetitorOffer, WebResearchRun
from apps.web_research.serializers import (
    CompetitorOfferSerializer, TenantWebResearchSettingsSerializer,
    WebResearchRunListSerializer, WebResearchRunSerializer,
)
from apps.web_research.search_context import get_tenant_research_settings
from apps.web_research.services import WebResearchService, enrichment_coverage
from apps.web_research.providers.registry import registered_search_providers
from apps.web_research.routing import search_provider_candidates


@extend_schema(tags=['Web research'])
class ProductWebResearchView(APIView):
    """POST starts a manual grounded web research run for one tenant product."""

    def post(self, request, product_pk: int):
        product = get_object_or_404(
            Product.objects.select_related('tenant', 'catalog_category'),
            pk=product_pk,
            tenant=request.tenant,
        )
        try:
            ProductEnrichmentService.ensure_product_auto_parts_eligible(request.tenant, product)
        except (AutoPartsEnrichmentDisabled, ProductIsNotAutoPart) as exc:
            return Response(
                {'status': 'error', 'code': 'web_research_unavailable', 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        provider = str(request.data.get('search_provider') or '').strip()
        run, created = WebResearchService.create_run(
            product,
            trigger=WebResearchRun.Trigger.MANUAL,
            generate_after=bool(request.data.get('generate_after')),
            search_provider=provider,
            purpose=WebResearchRun.Purpose.ENRICHMENT,
        )
        if created:
            from apps.web_research.tasks import run_web_research
            transaction.on_commit(lambda: run_web_research.delay(run.pk))
        return Response({
            'status': 'ok',
            'data': WebResearchRunSerializer(run).data,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

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

    def get(self, request):
        settings = get_tenant_research_settings(request.tenant)
        return Response({
            'status': 'ok',
            'data': TenantWebResearchSettingsSerializer(settings).data,
            'can_edit': self._can_edit(request),
        })

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

    def post(self, request, product_pk: int):
        product = get_object_or_404(
            Product.objects.select_related('tenant', 'catalog_category'),
            pk=product_pk, tenant=request.tenant,
        )
        research_settings = get_tenant_research_settings(request.tenant)
        if not research_settings.market_research_enabled:
            return Response(
                {'status': 'error', 'code': 'market_research_disabled',
                 'message': 'Исследование цен отключено в настройках организации.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        force = bool(request.data.get('force'))
        fresh_verified = CompetitorOffer.objects.filter(
            tenant=request.tenant,
            product=product,
            expires_at__gt=now(),
            review_status=CompetitorOffer.ReviewStatus.VERIFIED,
        ).count()
        if not force and fresh_verified >= min(3, research_settings.result_limit):
            latest = product.web_research_runs.filter(
                purpose__in=[WebResearchRun.Purpose.PRICING, WebResearchRun.Purpose.COMBINED],
            ).order_by('-created_at').first()
            return Response({
                'status': 'ok', 'reused': True,
                'message': 'Использованы свежие предложения; платный поиск не запускался.',
                'data': WebResearchRunSerializer(latest).data if latest else None,
            })
        run, created = WebResearchService.create_run(
            product,
            trigger=WebResearchRun.Trigger.MANUAL,
            search_provider=str(request.data.get('search_provider') or '').strip(),
            purpose=WebResearchRun.Purpose.PRICING,
        )
        if created:
            from apps.web_research.tasks import run_web_research
            transaction.on_commit(lambda: run_web_research.delay(run.pk))
        return Response({
            'status': 'ok', 'reused': not created,
            'data': WebResearchRunSerializer(run).data,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@extend_schema(tags=['Web research'])
class ProductMarketOfferListView(APIView):
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
    def get(self, request, listing_pk: int):
        listing = get_object_or_404(
            Listing.objects.select_related('tenant', 'product'),
            pk=listing_pk, tenant=request.tenant,
        )
        return Response({'status': 'ok', 'data': listing_market_comparison(listing)})
