from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.pagination import MapPagination
from apps.products.models import Product
from apps.products.serializers import ProductSerializer
from apps.products.tasks import import_from_datasource


@extend_schema(tags=['Products'])
class ProductListView(APIView):
    """
    GET /api/v1/products/ — список товаров тенанта с поиском и фильтрацией.

    Query params:
        search          — поиск по артикулу, названию, бренду
        export_enabled  — true/false, фильтр по флагу выгрузки
        category_1c     — точное совпадение категории из 1С
        page            — номер страницы (default: 1)
        page_size       — размер страницы (default: 50, max: 500)
    """

    def get(self, request):
        qs = Product.objects.filter(tenant=request.tenant).prefetch_related('images')

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(article__icontains=search)
                | Q(name__icontains=search)
                | Q(brand__icontains=search)
            )

        export_enabled = request.query_params.get('export_enabled')
        if export_enabled is not None:
            qs = qs.filter(export_enabled=export_enabled.lower() == 'true')

        category = request.query_params.get('category_1c', '').strip()
        if category:
            qs = qs.filter(category_1c=category)

        qs = qs.order_by('-sync_at', '-created_at')

        paginator = MapPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(ProductSerializer(page, many=True).data)


@extend_schema(tags=['Products'])
class ProductDetailView(APIView):
    """GET /api/v1/products/{pk}/ — карточка товара."""

    def get(self, request, pk):
        try:
            product = Product.objects.prefetch_related('images').get(
                pk=pk, tenant=request.tenant
            )
        except Product.DoesNotExist:
            return Response(
                {'status': 'error', 'code': 'not_found', 'message': 'Товар не найден'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({'status': 'ok', 'data': ProductSerializer(product).data})


class ProductSyncView(APIView):
    """POST /api/v1/products/sync/{connection_id}/ — запустить импорт товаров."""

    def post(self, request, connection_id):
        from apps.datasources.models import DataSourceConnection
        from django.shortcuts import get_object_or_404
        conn = get_object_or_404(DataSourceConnection, pk=connection_id, tenant=request.tenant)
        task = import_from_datasource.delay(conn.pk)
        return Response({'status': 'ok', 'data': {'task_id': task.id}})
