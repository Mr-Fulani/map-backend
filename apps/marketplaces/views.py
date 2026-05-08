from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.marketplaces.models import CategoryMapping
from apps.marketplaces.serializers import CategoryMappingSerializer, CategoryMappingWriteSerializer
from apps.marketplaces.services import CategoryMappingService


class UnmappedCategoriesView(APIView):
    def get(self, request):
        categories = CategoryMappingService.get_unmapped_categories(request.tenant)
        return Response({'unmapped': categories, 'count': len(categories)})


class CategoryMappingListView(APIView):
    def get(self, request):
        qs = CategoryMapping.objects.filter(tenant=request.tenant)
        return Response(CategoryMappingSerializer(qs, many=True).data)

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


class CategoryMappingDetailView(APIView):
    def get(self, request, pk):
        mapping = CategoryMapping.objects.get(pk=pk, tenant=request.tenant)
        return Response(CategoryMappingSerializer(mapping).data)

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

    def delete(self, request, pk):
        CategoryMapping.objects.filter(pk=pk, tenant=request.tenant).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
