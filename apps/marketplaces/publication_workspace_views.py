"""Lightweight, read-only data for the marketplace publication drawer.

The endpoint intentionally returns only local state.  It never contacts a
marketplace and never enters either provider's publication workflow.
"""

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.response import Response

from apps.image_search.serializers import ProductImageSerializer
from apps.marketplaces.listing_delivery import listing_publication_available
from apps.marketplaces.models import Listing, MarketplaceAccount, OzonOfferDraft
from apps.marketplaces.serializers import ListingSerializer, marketplace_label
from apps.products.models import Product
from apps.tenants.api_views import ListingsAPIView


PUBLICATION_WORKSPACE_RESPONSE = inline_serializer(
    name='MarketplacePublicationWorkspaceResponse',
    fields={
        'status': serializers.CharField(read_only=True),
        'data': serializers.DictField(read_only=True),
    },
)


def _datetime(value):
    if value is None:
        return None
    return serializers.DateTimeField().to_representation(value)


def _provider_error_count(value) -> int:
    return len(value) if isinstance(value, list) else 0


def _account_row(account: MarketplaceAccount) -> dict:
    return {
        'id': account.pk,
        'name': account.name,
        'marketplace': account.marketplace,
        'marketplace_label': marketplace_label(account.marketplace),
        'is_active': account.is_active,
    }


def _avito_row(listing: Listing, presenter: ListingSerializer) -> dict:
    # Full Avito validation remains owned by the protected Avito drawer.  The
    # workspace only needs enough lifecycle state to route to that drawer.
    return {
        'id': listing.pk,
        'account_id': listing.account_id,
        'status': listing.status,
        'status_display': presenter.get_status_display(listing),
        'can_publish': listing_publication_available(listing),
        'preflight_loaded': False,
    }


def _ozon_row(draft: OzonOfferDraft) -> dict:
    provider_sku = draft.provider_sku
    return {
        'id': draft.pk,
        'account_id': draft.account_id,
        'draft_exists': True,
        'publication_status': draft.publication_status,
        'provider_product_id': draft.provider_product_id,
        'provider_sku': provider_sku,
        'provider_status': draft.provider_status,
        'moderation_status': draft.moderation_status,
        'provider_error_count': _provider_error_count(draft.provider_errors),
        'last_provider_sync_at': _datetime(draft.last_provider_sync_at),
        'external_url': (
            f'https://www.ozon.ru/product/{provider_sku}/'
            if provider_sku is not None else ''
        ),
    }


def publication_workspace_snapshot(request, product: Product) -> dict:
    accounts = list(MarketplaceAccount.objects.filter(
        tenant=request.tenant,
        is_active=True,
        deleted_at__isnull=True,
        marketplace__in=(
            MarketplaceAccount.MARKETPLACE_AVITO,
            MarketplaceAccount.MARKETPLACE_OZON,
        ),
    ).order_by('marketplace', 'name', 'pk'))
    account_ids = [account.pk for account in accounts]

    images = product.images.exclude(status='rejected').order_by(
        '-is_primary', 'position', 'pk',
    )
    avito_listings = Listing.objects.filter(
        tenant=request.tenant,
        product=product,
        account_id__in=account_ids,
        account__tenant=request.tenant,
        account__is_active=True,
        account__deleted_at__isnull=True,
        account__marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
    ).exclude(status=Listing.STATUS_DELETED).select_related(
        'account', 'feed_run', 'product',
    ).order_by('account_id', '-updated_at', '-pk')
    ozon_drafts = OzonOfferDraft.objects.filter(
        tenant=request.tenant,
        product=product,
        account_id__in=account_ids,
        account__tenant=request.tenant,
        account__is_active=True,
        account__deleted_at__isnull=True,
        account__marketplace=MarketplaceAccount.MARKETPLACE_OZON,
    ).order_by('account_id', '-updated_at', '-pk')

    listing_presenter = ListingSerializer(context={'request': request})
    avito_by_account: dict[int, dict] = {}
    for listing in avito_listings:
        avito_by_account.setdefault(
            listing.account_id,
            _avito_row(listing, listing_presenter),
        )
    ozon_by_account: dict[int, dict] = {}
    for draft in ozon_drafts:
        ozon_by_account.setdefault(draft.account_id, _ozon_row(draft))

    return {
        'product': {
            'id': product.pk,
            'article': product.article,
            'name': product.name,
            'brand': product.brand or None,
            'price': str(product.price),
            'stock_qty': product.stock_qty,
            'title_ai': product.title_ai,
            'description_ai': product.description_ai,
        },
        'accounts': [_account_row(account) for account in accounts],
        'images': ProductImageSerializer(
            images,
            many=True,
            context={'request': request},
        ).data,
        'avito_listings': list(avito_by_account.values()),
        'ozon_drafts': list(ozon_by_account.values()),
    }


@extend_schema(tags=['Listings'])
class PublicationWorkspaceView(ListingsAPIView):
    """Open the local multi-marketplace drawer with a bounded query shape."""

    api_key_enabled = True

    @extend_schema(
        operation_id='marketplace_publication_workspace_retrieve',
        responses=PUBLICATION_WORKSPACE_RESPONSE,
    )
    def get(self, request, product_pk):
        product = Product.objects.filter(
            pk=product_pk,
            tenant=request.tenant,
        ).first()
        if product is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response({
            'status': 'ok',
            'data': publication_workspace_snapshot(request, product),
        })
