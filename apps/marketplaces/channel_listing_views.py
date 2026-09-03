"""Additive API for the provider-neutral marketplace channel index."""

from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiTypes,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers

from apps.core.pagination import MapPagination
from apps.marketplaces.channel_listing_index import (
    NORMALIZED_STATUSES,
    channel_index_keys,
    hydrate_channel_rows,
)
from apps.tenants.api_views import ListingsAPIView


class ChannelListingQuerySerializer(serializers.Serializer):
    marketplace = serializers.RegexField(
        r'^[a-z0-9_-]+$',
        required=False,
        allow_blank=True,
        max_length=50,
    )
    account = serializers.IntegerField(required=False, min_value=1)
    status = serializers.ChoiceField(
        choices=NORMALIZED_STATUSES,
        required=False,
        allow_blank=True,
    )


CHANNEL_LIST_RESPONSE = inline_serializer(
    name='MarketplaceChannelListingListResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'data': serializers.ListField(
            child=serializers.DictField(),
            read_only=True,
        ),
        'meta': inline_serializer(
            name='MarketplaceChannelListingPaginationMeta',
            fields={
                'total': serializers.IntegerField(read_only=True),
                'page': serializers.IntegerField(read_only=True),
                'page_size': serializers.IntegerField(read_only=True),
                'next': serializers.URLField(allow_null=True, read_only=True),
                'prev': serializers.URLField(allow_null=True, read_only=True),
            },
        ),
    },
)


@extend_schema(tags=['Listings'])
class ChannelListingListView(ListingsAPIView):
    """Read-only combined index; provider lifecycle models stay independent."""

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_channel_listing_list',
        parameters=[
            OpenApiParameter('status', OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter('account', OpenApiTypes.INT, OpenApiParameter.QUERY),
            OpenApiParameter('marketplace', OpenApiTypes.STR, OpenApiParameter.QUERY),
            OpenApiParameter('page', OpenApiTypes.INT, OpenApiParameter.QUERY),
            OpenApiParameter('page_size', OpenApiTypes.INT, OpenApiParameter.QUERY),
        ],
        responses=CHANNEL_LIST_RESPONSE,
    )
    def get(self, request):
        query = ChannelListingQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        filters = query.validated_data
        keys = channel_index_keys(
            request.tenant,
            marketplace=filters.get('marketplace', ''),
            account_id=filters.get('account'),
            normalized_status=filters.get('status', ''),
        )
        paginator = MapPagination()
        page = paginator.paginate_queryset(keys, request)
        if page is None:
            page = []
        data = hydrate_channel_rows(
            request.tenant,
            page,
            expected_status=filters.get('status', ''),
        )
        return paginator.get_paginated_response(data)
