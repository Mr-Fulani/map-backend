from rest_framework.response import Response
from rest_framework.views import APIView

from apps.products.models import Product
from apps.products.serializers import ProductSerializer
from apps.products.tasks import import_from_datasource


class ProductListView(APIView):
    def get(self, request):
        qs = Product.objects.filter(tenant=request.tenant).prefetch_related('images')
        serializer = ProductSerializer(qs, many=True)
        return Response(serializer.data)


class ProductDetailView(APIView):
    def get(self, request, pk):
        product = Product.objects.get(pk=pk, tenant=request.tenant)
        return Response(ProductSerializer(product).data)


class ProductSyncView(APIView):
    def post(self, request, connection_id):
        from apps.datasources.models import DataSourceConnection
        from django.shortcuts import get_object_or_404
        conn = get_object_or_404(DataSourceConnection, pk=connection_id, tenant=request.tenant)
        task = import_from_datasource.delay(conn.pk)
        return Response({'task_id': task.id})
