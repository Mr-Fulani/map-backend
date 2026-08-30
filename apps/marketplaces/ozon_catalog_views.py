from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.marketplaces.models import MarketplaceAccount, OzonCategoryTreeSnapshot
from apps.marketplaces.ozon_catalog import OzonCatalogError, OzonCatalogService
from apps.core.pagination import MapPagination
from apps.tenants.api_views import ListingsAPIView as APIView
from apps.tenants.permissions import TenantAdminWritePermission


class OzonCatalogRefreshSerializer(serializers.Serializer):
    scope = serializers.ChoiceField(choices=['tree', 'attributes'])
    language = serializers.ChoiceField(
        choices=[choice[0] for choice in OzonCategoryTreeSnapshot.LANGUAGE_CHOICES],
        default=OzonCategoryTreeSnapshot.LANGUAGE_DEFAULT,
    )
    description_category_id = serializers.IntegerField(
        min_value=1,
        required=False,
    )
    type_id = serializers.IntegerField(min_value=1, required=False)
    confirm_ozon_read_only_access = serializers.BooleanField()

    def validate(self, attrs):
        if attrs['confirm_ozon_read_only_access'] is not True:
            raise serializers.ValidationError({
                'confirm_ozon_read_only_access': (
                    'Подтвердите read-only обновление справочника Ozon.'
                ),
            })
        if attrs['scope'] == 'attributes':
            missing = [
                field for field in ('description_category_id', 'type_id')
                if field not in attrs
            ]
            if missing:
                raise serializers.ValidationError({
                    field: 'Обязательное поле для схемы характеристик.'
                    for field in missing
                })
        elif 'description_category_id' in attrs or 'type_id' in attrs:
            raise serializers.ValidationError({
                'scope': 'ID категории и типа допустимы только для attributes.',
            })
        return attrs


CATALOG_TREE_METADATA = inline_serializer(
    name='OzonCatalogTreeMetadata',
    allow_null=True,
    fields={
        'revision': serializers.CharField(read_only=True),
        'language': serializers.CharField(read_only=True),
        'node_count': serializers.IntegerField(read_only=True),
        'active_type_count': serializers.IntegerField(read_only=True),
        'first_synced_at': serializers.DateTimeField(read_only=True),
        'last_checked_at': serializers.DateTimeField(read_only=True),
    },
)

CATALOG_ATTRIBUTE_METADATA = inline_serializer(
    name='OzonCatalogAttributeMetadata',
    allow_null=True,
    fields={
        'revision': serializers.CharField(read_only=True),
        'description_category_id': serializers.IntegerField(read_only=True),
        'type_id': serializers.IntegerField(read_only=True),
        'language': serializers.CharField(read_only=True),
        'attribute_count': serializers.IntegerField(read_only=True),
        'required_attribute_count': serializers.IntegerField(read_only=True),
        'first_synced_at': serializers.DateTimeField(read_only=True),
        'last_checked_at': serializers.DateTimeField(read_only=True),
    },
)

CATALOG_STATE_RESPONSE = inline_serializer(
    name='OzonCatalogState',
    fields={
        'account_id': serializers.IntegerField(read_only=True),
        'marketplace': serializers.CharField(read_only=True),
        'tree': CATALOG_TREE_METADATA,
        'attribute_schema_count': serializers.IntegerField(read_only=True),
        'latest_attribute_schema': CATALOG_ATTRIBUTE_METADATA,
    },
)


class OzonCatalogTypesQuerySerializer(serializers.Serializer):
    search = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=120,
        trim_whitespace=True,
    )
    language = serializers.ChoiceField(
        choices=[choice[0] for choice in OzonCategoryTreeSnapshot.LANGUAGE_CHOICES],
        default=OzonCategoryTreeSnapshot.LANGUAGE_DEFAULT,
    )


class OzonCatalogTreeLevelQuerySerializer(serializers.Serializer):
    parent = serializers.CharField(  # type: ignore[assignment]
        required=False,
        allow_blank=True,
        max_length=400,
        trim_whitespace=True,
    )
    language = serializers.ChoiceField(
        choices=[choice[0] for choice in OzonCategoryTreeSnapshot.LANGUAGE_CHOICES],
        default=OzonCategoryTreeSnapshot.LANGUAGE_DEFAULT,
    )

    def validate_parent(self, value):
        if not value:
            return ()
        raw_ids = value.split(',')
        if len(raw_ids) > settings.OZON_CATALOG_MAX_DEPTH:
            raise serializers.ValidationError('Путь категории Ozon слишком глубокий.')
        if any(not item.isascii() or not item.isdecimal() for item in raw_ids):
            raise serializers.ValidationError('Некорректный путь категории Ozon.')
        parent_ids = tuple(int(item) for item in raw_ids)
        if any(item <= 0 for item in parent_ids):
            raise serializers.ValidationError('Некорректный путь категории Ozon.')
        return parent_ids


class OzonAttributeValueSearchSerializer(serializers.Serializer):
    description_category_id = serializers.IntegerField(min_value=1)
    type_id = serializers.IntegerField(min_value=1)
    attribute_id = serializers.IntegerField(min_value=1)
    query = serializers.CharField(
        min_length=2,
        max_length=120,
        trim_whitespace=True,
    )
    language = serializers.ChoiceField(
        choices=[
            choice[0]
            for choice in OzonCategoryTreeSnapshot.LANGUAGE_CHOICES
        ],
        default=OzonCategoryTreeSnapshot.LANGUAGE_DEFAULT,
    )
    confirm_ozon_read_only_access = serializers.BooleanField()

    def validate_confirm_ozon_read_only_access(self, value):
        if value is not True:
            raise serializers.ValidationError(
                'Подтвердите read-only поиск в справочнике Ozon.',
            )
        return value


class OzonCatalogTypePagination(MapPagination):
    page_size = 25
    max_page_size = 50


CATALOG_TYPE_ITEMS = inline_serializer(
    name='OzonCatalogTypeItem',
    many=True,
    fields={
        'description_category_id': serializers.IntegerField(read_only=True),
        'type_id': serializers.IntegerField(read_only=True),
        'category_path': serializers.CharField(read_only=True),
        'type_name': serializers.CharField(read_only=True),
    },
)

CATALOG_TYPES_RESPONSE = inline_serializer(
    name='OzonCatalogTypesResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'data': CATALOG_TYPE_ITEMS,
        'meta': inline_serializer(
            name='OzonCatalogTypesMeta',
            fields={
                'total': serializers.IntegerField(read_only=True),
                'page': serializers.IntegerField(read_only=True),
                'page_size': serializers.IntegerField(read_only=True),
                'next': serializers.URLField(allow_null=True, read_only=True),
                'prev': serializers.URLField(allow_null=True, read_only=True),
                'tree_revision': serializers.CharField(
                    allow_null=True,
                    read_only=True,
                ),
                'tree_checked_at': serializers.DateTimeField(
                    allow_null=True,
                    read_only=True,
                ),
                'language': serializers.CharField(read_only=True),
            },
        ),
    },
)

CATALOG_TREE_PATH_ITEMS = inline_serializer(
    name='OzonCatalogTreePathItem',
    many=True,
    fields={
        'description_category_id': serializers.IntegerField(read_only=True),
        'name': serializers.CharField(read_only=True),
    },
)

CATALOG_TREE_OPTIONS = inline_serializer(
    name='OzonCatalogTreeOption',
    many=True,
    fields={
        'kind': serializers.CharField(read_only=True),
        'description_category_id': serializers.IntegerField(read_only=True),
        'type_id': serializers.IntegerField(allow_null=True, read_only=True),
        'name': serializers.CharField(read_only=True),
        'category_path': serializers.CharField(read_only=True),
    },
)

CATALOG_TREE_LEVEL_RESPONSE = inline_serializer(
    name='OzonCatalogTreeLevelResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'data': inline_serializer(
            name='OzonCatalogTreeLevelData',
            fields={
                'path': CATALOG_TREE_PATH_ITEMS,
                'options': CATALOG_TREE_OPTIONS,
                'tree_revision': serializers.CharField(
                    allow_null=True,
                    read_only=True,
                ),
            },
        ),
    },
)


@extend_schema(tags=['Accounts'])
class OzonCatalogView(APIView):
    """Read/sync Ozon catalog metadata without entering Avito workflows."""

    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @staticmethod
    def _account(request, pk):
        return MarketplaceAccount.objects.filter(
            pk=pk,
            tenant=request.tenant,
            marketplace=MarketplaceAccount.MARKETPLACE_OZON,
        ).first()

    @extend_schema(
        operation_id='ozon_catalog_state_retrieve',
        responses=CATALOG_STATE_RESPONSE,
    )
    def get(self, request, pk):
        account = self._account(request, pk)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(OzonCatalogService.state(account))

    @extend_schema(
        operation_id='ozon_catalog_refresh',
        request=OzonCatalogRefreshSerializer,
        responses=CATALOG_STATE_RESPONSE,
    )
    def post(self, request, pk):
        account = self._account(request, pk)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = OzonCatalogRefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            if data['scope'] == 'tree':
                OzonCatalogService.sync_tree(
                    account,
                    language=data['language'],
                    confirmed=data['confirm_ozon_read_only_access'],
                )
            else:
                OzonCatalogService.sync_attributes(
                    account,
                    description_category_id=data['description_category_id'],
                    type_id=data['type_id'],
                    language=data['language'],
                    confirmed=data['confirm_ozon_read_only_access'],
                )
        except OzonCatalogError as exc:
            response_status = {
                'provider_disabled': status.HTTP_503_SERVICE_UNAVAILABLE,
                'rate_limited': status.HTTP_429_TOO_MANY_REQUESTS,
            }.get(exc.code, status.HTTP_400_BAD_REQUEST)
            payload: dict[str, object] = {
                'status': 'error',
                'code': exc.code,
                'message': str(exc),
            }
            if exc.retry_after_seconds is not None:
                payload['retry_after_seconds'] = exc.retry_after_seconds
            return Response(payload, status=response_status)
        return Response(OzonCatalogService.state(account))


@extend_schema(tags=['Accounts'])
class OzonCatalogTypesView(APIView):
    """Browse one account's latest local Ozon tree without provider I/O."""

    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(
        operation_id='ozon_catalog_types_list',
        parameters=[
            OpenApiParameter(
                'search', OpenApiTypes.STR, OpenApiParameter.QUERY,
                description='Поиск по пути категории, типу товара или Ozon ID.',
            ),
            OpenApiParameter(
                'language', OpenApiTypes.STR, OpenApiParameter.QUERY,
                description='Язык локального снимка дерева.',
            ),
            OpenApiParameter('page', OpenApiTypes.INT, OpenApiParameter.QUERY),
            OpenApiParameter('page_size', OpenApiTypes.INT, OpenApiParameter.QUERY),
        ],
        responses=CATALOG_TYPES_RESPONSE,
    )
    def get(self, request, pk):
        account = OzonCatalogView._account(request, pk)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = OzonCatalogTypesQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            snapshot, category_types = OzonCatalogService.category_types(
                account,
                language=data['language'],
                search=data.get('search', ''),
            )
        except OzonCatalogError as exc:
            return Response({
                'status': 'error',
                'code': exc.code,
                'message': str(exc),
            }, status=status.HTTP_409_CONFLICT)

        paginator = OzonCatalogTypePagination()
        page = paginator.paginate_sequence(category_types, request)
        response = paginator.get_paginated_response(page)
        response.data['meta'].update({
            'tree_revision': snapshot.schema_hash if snapshot else None,
            'tree_checked_at': snapshot.updated_at if snapshot else None,
            'language': data['language'],
        })
        return response


@extend_schema(tags=['Accounts'])
class OzonCatalogTreeLevelView(APIView):
    """Browse one level of the latest local Ozon tree without provider I/O."""

    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(
        operation_id='ozon_catalog_tree_level',
        parameters=[
            OpenApiParameter(
                'parent',
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                description='Путь ID разделов через запятую; пусто для корня.',
            ),
            OpenApiParameter(
                'language', OpenApiTypes.STR, OpenApiParameter.QUERY,
            ),
        ],
        responses=CATALOG_TREE_LEVEL_RESPONSE,
    )
    def get(self, request, pk):
        account = OzonCatalogView._account(request, pk)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = OzonCatalogTreeLevelQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            snapshot, level = OzonCatalogService.category_tree_level(
                account,
                language=data['language'],
                parent_ids=data.get('parent', ()),
            )
        except OzonCatalogError as exc:
            return Response({
                'status': 'error',
                'code': exc.code,
                'message': str(exc),
            }, status=status.HTTP_409_CONFLICT)
        return Response({
            'status': 'ok',
            'data': {
                **level,
                'tree_revision': snapshot.schema_hash if snapshot else None,
            },
        })


@extend_schema(tags=['Accounts'])
class OzonAttributeValueSearchView(APIView):
    """Explicit bounded dictionary search; never changes an Ozon product."""

    permission_classes = [IsAuthenticated, TenantAdminWritePermission]

    @extend_schema(
        operation_id='ozon_catalog_attribute_values_search',
        request=OzonAttributeValueSearchSerializer,
        responses=inline_serializer(
            name='OzonAttributeValueSearchResponse',
            fields={
                'status': serializers.CharField(read_only=True),
                'data': inline_serializer(
                    name='OzonAttributeValueSearchData',
                    fields={
                        'revision': serializers.CharField(read_only=True),
                        'query': serializers.CharField(read_only=True),
                        'values': serializers.ListField(
                            child=serializers.DictField(),
                            read_only=True,
                        ),
                    },
                ),
            },
        ),
    )
    def post(self, request, pk):
        account = OzonCatalogView._account(request, pk)
        if account is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        serializer = OzonAttributeValueSearchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            snapshot = OzonCatalogService.search_attribute_values(
                account,
                description_category_id=data['description_category_id'],
                type_id=data['type_id'],
                attribute_id=data['attribute_id'],
                query=data['query'],
                language=data['language'],
                confirmed=data['confirm_ozon_read_only_access'],
            )
        except OzonCatalogError as exc:
            response_status = {
                'provider_disabled': status.HTTP_503_SERVICE_UNAVAILABLE,
                'rate_limited': status.HTTP_429_TOO_MANY_REQUESTS,
            }.get(exc.code, status.HTTP_400_BAD_REQUEST)
            return Response(
                {'status': 'error', 'code': exc.code, 'message': str(exc)},
                status=response_status,
            )
        return Response({
            'status': 'ok',
            'data': {
                'revision': snapshot.schema_hash,
                'query': snapshot.query,
                'values': snapshot.values,
            },
        })
