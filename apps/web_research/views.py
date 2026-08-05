from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pagination import MapPagination
from apps.products.models import Product
from apps.products.services import (
    AutoPartsEnrichmentDisabled, ProductEnrichmentService, ProductIsNotAutoPart,
)
from apps.web_research.models import WebResearchRun
from apps.web_research.serializers import (
    WebResearchRunListSerializer, WebResearchRunSerializer,
)
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
        run = product.web_research_runs.order_by('-created_at').first()
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
