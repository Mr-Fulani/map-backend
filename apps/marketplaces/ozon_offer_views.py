from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.response import Response

from apps.marketplaces.models import MarketplaceAccount
from apps.marketplaces.ozon_offers import (
    OzonOfferError,
    offer_presentation,
    update_offer_draft,
)
from apps.products.models import Product
from apps.tenants.api_views import CatalogAPIView as APIView


class OzonOfferAccountQuerySerializer(serializers.Serializer):
    account_id = serializers.IntegerField(min_value=1)


class OzonOfferUpdateSerializer(serializers.Serializer):
    account_id = serializers.IntegerField(min_value=1)
    description_category_id = serializers.IntegerField(min_value=1, required=False)
    type_id = serializers.IntegerField(min_value=1, required=False)
    attributes = serializers.JSONField(required=False)

    def validate(self, attrs):
        allowed = {
            'account_id',
            'description_category_id',
            'type_id',
            'attributes',
        }
        unknown = set(self.initial_data) - allowed
        if unknown:
            raise serializers.ValidationError(
                f'Неподдерживаемые поля: {", ".join(sorted(unknown))}',
            )
        category_fields = {
            field for field in ('description_category_id', 'type_id')
            if field in attrs
        }
        if category_fields and len(category_fields) != 2:
            raise serializers.ValidationError(
                'ID категории и типа Ozon нужно передать вместе.',
            )
        return attrs


OZON_OFFER_RESPONSE = inline_serializer(
    name='OzonOfferPreparationResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'data': serializers.DictField(read_only=True),
    },
)


@extend_schema(tags=['Products'])
class ProductOzonOfferView(APIView):
    """Local Ozon preparation only; no Listing or provider mutation."""

    api_key_enabled = True
    api_key_scopes = {
        'GET': {'catalog:read'},
        'HEAD': {'catalog:read'},
        'OPTIONS': {'catalog:read'},
        'PATCH': {'catalog:write'},
    }

    @staticmethod
    def _product(request, pk):
        queryset = Product.objects.select_related('physical_profile')
        return queryset.filter(pk=pk, tenant=request.tenant).first()

    @staticmethod
    def _account(request, account_id):
        return MarketplaceAccount.objects.filter(
            pk=account_id,
            tenant=request.tenant,
            marketplace=MarketplaceAccount.MARKETPLACE_OZON,
        ).first()

    @extend_schema(
        operation_id='product_ozon_offer_retrieve',
        parameters=[OpenApiParameter('account_id', OpenApiTypes.INT)],
        responses=OZON_OFFER_RESPONSE,
    )
    def get(self, request, pk):
        query = OzonOfferAccountQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        product = self._product(request, pk)
        account = self._account(request, query.validated_data['account_id'])
        if product is None or account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response({'status': 'ok', 'data': offer_presentation(product, account)})

    @extend_schema(
        operation_id='product_ozon_offer_update',
        request=OzonOfferUpdateSerializer,
        responses=OZON_OFFER_RESPONSE,
    )
    def patch(self, request, pk):
        serializer = OzonOfferUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        product = self._product(request, pk)
        account = self._account(request, data['account_id'])
        if product is None or account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        category = None
        if 'description_category_id' in data:
            category = (data['description_category_id'], data['type_id'])
        try:
            update_offer_draft(
                product,
                account,
                category=category,
                attributes=data.get('attributes'),
                attributes_supplied='attributes' in data,
            )
        except OzonOfferError as exc:
            return Response(
                {'status': 'error', 'code': exc.code, 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'ok', 'data': offer_presentation(product, account)})
