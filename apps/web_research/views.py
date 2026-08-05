from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.products.models import Product
from apps.products.services import (
    AutoPartsEnrichmentDisabled, ProductEnrichmentService, ProductIsNotAutoPart,
)
from apps.web_research.models import WebResearchRun
from apps.web_research.serializers import WebResearchRunSerializer
from apps.web_research.services import WebResearchService, enrichment_coverage


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
