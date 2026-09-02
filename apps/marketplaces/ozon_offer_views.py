from decimal import Decimal

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.response import Response

from apps.marketplaces.models import MarketplaceAccount
from apps.marketplaces.ozon_autofill import OzonAutofillError, autofill_ozon_offer
from apps.marketplaces.ozon_offers import (
    OzonOfferError,
    offer_presentation,
    update_offer_draft,
)
from apps.marketplaces.ozon_publication import (
    OzonPublicationError,
    request_product_import,
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
    margin_pct = serializers.DecimalField(
        max_digits=7,
        decimal_places=2,
        min_value=Decimal('-99.99'),
        required=False,
        allow_null=True,
    )
    price_override = serializers.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal('0.01'),
        required=False,
        allow_null=True,
    )

    def validate(self, attrs):
        allowed = {
            'account_id',
            'description_category_id',
            'type_id',
            'attributes',
            'margin_pct',
            'price_override',
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
        if attrs.get('margin_pct') is not None and attrs.get('price_override') is not None:
            raise serializers.ValidationError(
                'Передайте либо индивидуальную наценку, либо точную цену Ozon.',
            )
        return attrs


class OzonOfferPublishSerializer(serializers.Serializer):
    account_id = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.UUIDField()


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
        'POST': {'catalog:write'},
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
                margin_pct=data.get('margin_pct'),
                margin_supplied='margin_pct' in data,
                price_override=data.get('price_override'),
                price_supplied='price_override' in data,
            )
        except OzonOfferError as exc:
            return Response(
                {'status': 'error', 'code': exc.code, 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'ok', 'data': offer_presentation(product, account)})

    @extend_schema(
        operation_id='product_ozon_offer_autofill',
        request=OzonOfferAccountQuerySerializer,
        responses=OZON_OFFER_RESPONSE,
    )
    def post(self, request, pk):
        serializer = OzonOfferAccountQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = self._product(request, pk)
        account = self._account(request, serializer.validated_data['account_id'])
        if product is None or account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            autofill_ozon_offer(product, account, allow_provider_reads=True)
        except OzonAutofillError as exc:
            return Response(
                {'status': 'error', 'code': exc.code, 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'ok', 'data': offer_presentation(product, account)})


@extend_schema(tags=['Products'])
class ProductOzonOfferPublishView(APIView):
    """Manual single-offer Ozon write with durable idempotency evidence."""

    api_key_enabled = True
    api_key_scopes = {'POST': {'catalog:write'}}

    @extend_schema(
        operation_id='product_ozon_offer_publish',
        request=OzonOfferPublishSerializer,
        responses=OZON_OFFER_RESPONSE,
    )
    def post(self, request, pk):
        serializer = OzonOfferPublishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = Product.objects.filter(pk=pk, tenant=request.tenant).first()
        account = MarketplaceAccount.objects.filter(
            pk=serializer.validated_data['account_id'],
            tenant=request.tenant,
            marketplace=MarketplaceAccount.MARKETPLACE_OZON,
        ).first()
        if product is None or account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        try:
            operation = request_product_import(
                product,
                account,
                idempotency_key=str(serializer.validated_data['idempotency_key']),
            )
        except OzonPublicationError as exc:
            return Response(
                {'status': 'error', 'code': exc.code, 'message': str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({
            'status': 'ok',
            'data': offer_presentation(product, account),
            'operation_id': str(operation.pk),
        }, status=status.HTTP_202_ACCEPTED)
