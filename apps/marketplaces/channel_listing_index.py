"""Read-only provider-neutral index for tenant marketplace channels.

The existing ``Listing`` model and API remain the Avito lifecycle source of
truth.  Ozon keeps its independent ``OzonOfferDraft`` lifecycle.  This module
combines only lightweight keys in SQL, paginates those keys, then hydrates at
most one bounded batch per resource type.
"""

from collections.abc import Iterable, Mapping
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db.models import (
    BigIntegerField,
    Case,
    CharField,
    Count,
    IntegerField,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.functions import Cast
from rest_framework import serializers

from apps.marketplaces.models import (
    Listing,
    MarketplaceAccount,
    OzonOfferDraft,
)
from apps.marketplaces.provider_registry import provider_capabilities
from apps.marketplaces.serializers import ListingSerializer


RESOURCE_LISTING = 'listing'
RESOURCE_OZON_OFFER = 'ozon_offer'

OZON_STATUS_DRAFT = ('local_draft',)
OZON_STATUS_QUEUED = ('queued',)
OZON_STATUS_PENDING = ('import_processing', 'moderation_pending', 'outcome_unknown')
OZON_STATUS_ACTIVE = ('published',)
OZON_STATUS_REJECTED = (
    'send_failed',
    'not_accepted',
    'import_failed',
    'moderation_failed',
)
OZON_STATUS_REVIEW = ('manual_review',)
OZON_STATUS_ARCHIVED = ('archived',)

OZON_KNOWN_PUBLICATION_STATUSES = (
    *OZON_STATUS_DRAFT,
    *OZON_STATUS_QUEUED,
    *OZON_STATUS_PENDING,
    *OZON_STATUS_ACTIVE,
    *OZON_STATUS_REJECTED,
    *OZON_STATUS_REVIEW,
    *OZON_STATUS_ARCHIVED,
)

NORMALIZED_STATUSES = (
    Listing.STATUS_DRAFT,
    Listing.STATUS_QUEUED,
    Listing.STATUS_PENDING,
    Listing.STATUS_ACTIVE,
    Listing.STATUS_REJECTED,
    Listing.STATUS_ARCHIVING,
    Listing.STATUS_ARCHIVED,
    Listing.STATUS_REQUIRES_REVIEW,
    Listing.STATUS_LIMIT_REACHED,
)


def _ozon_normalized_status_expression() -> Case:
    return Case(
        When(
            Q(
                publication_status__in=OZON_STATUS_REJECTED,
                provider_product_id__isnull=False,
                last_provider_sync_at__isnull=False,
            ),
            then=Value(Listing.STATUS_REQUIRES_REVIEW),
        ),
        When(publication_status__in=OZON_STATUS_DRAFT, then=Value(Listing.STATUS_DRAFT)),
        When(publication_status__in=OZON_STATUS_QUEUED, then=Value(Listing.STATUS_QUEUED)),
        When(publication_status__in=OZON_STATUS_PENDING, then=Value(Listing.STATUS_PENDING)),
        When(publication_status__in=OZON_STATUS_ACTIVE, then=Value(Listing.STATUS_ACTIVE)),
        When(publication_status__in=OZON_STATUS_REJECTED, then=Value(Listing.STATUS_REJECTED)),
        When(publication_status__in=OZON_STATUS_ARCHIVED, then=Value(Listing.STATUS_ARCHIVED)),
        When(publication_status__in=OZON_STATUS_REVIEW, then=Value(Listing.STATUS_REQUIRES_REVIEW)),
        default=Value(Listing.STATUS_REQUIRES_REVIEW),
        output_field=CharField(max_length=32),
    )


def _filter_ozon_status(qs: QuerySet, normalized_status: str) -> QuerySet:
    source_statuses = {
        Listing.STATUS_DRAFT: OZON_STATUS_DRAFT,
        Listing.STATUS_QUEUED: OZON_STATUS_QUEUED,
        Listing.STATUS_PENDING: OZON_STATUS_PENDING,
        Listing.STATUS_ACTIVE: OZON_STATUS_ACTIVE,
        Listing.STATUS_ARCHIVED: OZON_STATUS_ARCHIVED,
    }.get(normalized_status)
    if source_statuses is not None:
        return qs.filter(publication_status__in=source_statuses)
    if normalized_status == Listing.STATUS_REJECTED:
        return qs.filter(publication_status__in=OZON_STATUS_REJECTED).filter(
            Q(provider_product_id__isnull=True)
            | Q(last_provider_sync_at__isnull=True),
        )
    if normalized_status == Listing.STATUS_REQUIRES_REVIEW:
        return qs.filter(
            Q(publication_status__in=OZON_STATUS_REVIEW)
            | Q(
                publication_status__in=OZON_STATUS_REJECTED,
                provider_product_id__isnull=False,
                last_provider_sync_at__isnull=False,
            )
            | ~Q(publication_status__in=OZON_KNOWN_PUBLICATION_STATUSES),
        )
    # Ozon currently has no equivalent for Avito-specific archiving/limit
    # states. Keep the SQL branch valid while returning no rows.
    return qs.filter(pk__isnull=True)


def _avito_channel_queryset(tenant) -> QuerySet:
    return Listing.objects.filter(
        tenant=tenant,
        product__tenant=tenant,
        account__tenant=tenant,
        account__is_active=True,
        account__deleted_at__isnull=True,
        account__marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
    ).exclude(status=Listing.STATUS_DELETED)


def _ozon_channel_queryset(tenant) -> QuerySet:
    return OzonOfferDraft.objects.filter(
        tenant=tenant,
        product__tenant=tenant,
        account__tenant=tenant,
        account__is_active=True,
        account__deleted_at__isnull=True,
        account__marketplace=MarketplaceAccount.MARKETPLACE_OZON,
    ).exclude(
        # Enrichment creates technical drafts before a tenant hands the item
        # to Listings. They are not publication attempts and must not inflate
        # customer-facing marketplace counters.
        publication_status='local_draft',
    )


def channel_status_counts(tenant) -> dict[str, int]:
    """Aggregate the same provider-neutral lifecycle shown by Listings."""
    avito = _avito_channel_queryset(tenant).aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(status=Listing.STATUS_ACTIVE)),
        queued=Count('id', filter=Q(status=Listing.STATUS_QUEUED)),
        pending=Count('id', filter=Q(status=Listing.STATUS_PENDING)),
        rejected=Count('id', filter=Q(status=Listing.STATUS_REJECTED)),
        requires_review=Count(
            'id', filter=Q(status=Listing.STATUS_REQUIRES_REVIEW),
        ),
        limit_reached=Count(
            'id', filter=Q(status=Listing.STATUS_LIMIT_REACHED),
        ),
    )
    rejected_without_projection = Q(
        publication_status__in=OZON_STATUS_REJECTED,
    ) & (
        Q(provider_product_id__isnull=True)
        | Q(last_provider_sync_at__isnull=True)
    )
    requires_review = (
        Q(publication_status__in=OZON_STATUS_REVIEW)
        | (
            Q(publication_status__in=OZON_STATUS_REJECTED)
            & Q(provider_product_id__isnull=False)
            & Q(last_provider_sync_at__isnull=False)
        )
        | ~Q(publication_status__in=OZON_KNOWN_PUBLICATION_STATUSES)
    )
    ozon = _ozon_channel_queryset(tenant).aggregate(
        total=Count('id'),
        active=Count('id', filter=Q(publication_status__in=OZON_STATUS_ACTIVE)),
        queued=Count('id', filter=Q(publication_status__in=OZON_STATUS_QUEUED)),
        pending=Count('id', filter=Q(publication_status__in=OZON_STATUS_PENDING)),
        rejected=Count('id', filter=rejected_without_projection),
        requires_review=Count('id', filter=requires_review),
    )
    keys = ('total', 'active', 'queued', 'pending', 'rejected', 'requires_review')
    counts = {
        key: int(avito.get(key) or 0) + int(ozon.get(key) or 0)
        for key in keys
    }
    counts['limit_reached'] = int(avito.get('limit_reached') or 0)
    counts['avito_active'] = int(avito.get('active') or 0)
    counts['ozon_active'] = int(ozon.get('active') or 0)
    return counts


def channel_index_keys(
    tenant,
    *,
    marketplace: str = '',
    account_id: int | None = None,
    normalized_status: str = '',
) -> QuerySet:
    """Return a UNION ALL of lightweight, tenant-fenced resource keys."""
    avito = _avito_channel_queryset(tenant)
    ozon = _ozon_channel_queryset(tenant)

    if marketplace and marketplace != MarketplaceAccount.MARKETPLACE_AVITO:
        avito = avito.filter(pk__isnull=True)
    if marketplace and marketplace != MarketplaceAccount.MARKETPLACE_OZON:
        ozon = ozon.filter(pk__isnull=True)
    if account_id is not None:
        avito = avito.filter(account_id=account_id)
        ozon = ozon.filter(account_id=account_id)
    if normalized_status:
        avito = avito.filter(status=normalized_status)
        ozon = _filter_ozon_status(ozon, normalized_status)

    avito_keys = (
        avito.order_by()
        .annotate(
            resource_kind=Value(RESOURCE_LISTING, output_field=CharField(max_length=20)),
            resource_id=Cast('id', output_field=BigIntegerField()),
            source_rank=Value(0, output_field=IntegerField()),
            normalized_status=Cast('status', output_field=CharField(max_length=32)),
        )
        .values(
            'resource_kind', 'resource_id', 'source_rank', 'created_at',
            'normalized_status',
        )
    )
    ozon_keys = (
        ozon.order_by()
        .annotate(
            resource_kind=Value(RESOURCE_OZON_OFFER, output_field=CharField(max_length=20)),
            resource_id=Cast('id', output_field=BigIntegerField()),
            source_rank=Value(1, output_field=IntegerField()),
            normalized_status=_ozon_normalized_status_expression(),
        )
        .values(
            'resource_kind', 'resource_id', 'source_rank', 'created_at',
            'normalized_status',
        )
    )
    return avito_keys.union(ozon_keys, all=True).order_by(
        '-created_at', 'source_rank', '-resource_id',
    )


def _format_datetime(value) -> str | None:
    if value is None:
        return None
    return serializers.DateTimeField().to_representation(value)


def _format_price(value: Decimal) -> str:
    return str(Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _ozon_price(draft: OzonOfferDraft) -> Decimal:
    if draft.publication_status == 'published' and draft.last_synced_price is not None:
        return draft.last_synced_price
    if draft.price_override is not None:
        return draft.price_override
    if draft.last_synced_price is not None:
        return draft.last_synced_price
    # Category pricing is deliberately not resolved per row here: that would
    # turn a list page into N policy queries. The drawer remains authoritative
    # for the calculated pre-publication price.
    return draft.product.price


def _ozon_status(draft: OzonOfferDraft) -> str:
    source = draft.publication_status
    if (
        source in OZON_STATUS_REJECTED
        and draft.provider_product_id is not None
        and draft.last_provider_sync_at is not None
    ):
        return Listing.STATUS_REQUIRES_REVIEW
    if source in OZON_STATUS_DRAFT:
        return Listing.STATUS_DRAFT
    if source in OZON_STATUS_QUEUED:
        return Listing.STATUS_QUEUED
    if source in OZON_STATUS_PENDING:
        return Listing.STATUS_PENDING
    if source in OZON_STATUS_ACTIVE:
        return Listing.STATUS_ACTIVE
    if source in OZON_STATUS_REJECTED:
        return Listing.STATUS_REJECTED
    if source in OZON_STATUS_ARCHIVED:
        return Listing.STATUS_ARCHIVED
    return Listing.STATUS_REQUIRES_REVIEW


def _ozon_status_display(status: str) -> str:
    return {
        Listing.STATUS_DRAFT: 'Черновик',
        Listing.STATUS_QUEUED: 'В очереди',
        Listing.STATUS_PENDING: 'Обрабатывается Ozon',
        Listing.STATUS_ACTIVE: 'Активно',
        Listing.STATUS_REJECTED: 'Отклонено Ozon',
        Listing.STATUS_ARCHIVED: 'В архиве',
        Listing.STATUS_REQUIRES_REVIEW: 'Требует проверки',
    }.get(status, status)


def _ozon_status_explanation(draft: OzonOfferDraft, status: str) -> str:
    if status == Listing.STATUS_ACTIVE:
        return 'Ozon подтвердил публикацию карточки. Цена и остаток сверяются отдельно.'
    if status == Listing.STATUS_PENDING:
        if draft.provider_product_id is not None and draft.last_provider_sync_at is not None:
            return (
                'Ozon проверяет обновление. Ранее подтверждённая версия карточки '
                'может оставаться опубликованной.'
            )
        return 'Карточка отправлена и обрабатывается Ozon.'
    if status == Listing.STATUS_REJECTED:
        return 'Ozon не принял карточку. Откройте её и исправьте указанные ошибки.'
    if status == Listing.STATUS_REQUIRES_REVIEW:
        if (
            draft.publication_status in OZON_STATUS_REJECTED
            and draft.provider_product_id is not None
            and draft.last_provider_sync_at is not None
        ):
            return (
                'Ozon не принял последнее обновление, но ранее подтверждённая '
                'версия карточки может оставаться опубликованной. Сверьте её с Ozon.'
            )
        return 'MAP не может однозначно определить состояние карточки Ozon. Проверьте её вручную.'
    if status == Listing.STATUS_ARCHIVED:
        return 'Карточка снята с продажи в Ozon.'
    if draft.publication_status == 'local_draft':
        return 'Карточка подготовлена локально и ещё не отправлена в Ozon.'
    return ''


def _ozon_error_text(provider_errors: Any) -> str:
    if not isinstance(provider_errors, list):
        return ''
    messages: list[str] = []
    for item in provider_errors[:3]:
        if isinstance(item, Mapping):
            message = item.get('message') or item.get('description') or item.get('code')
        else:
            message = item
        if isinstance(message, str) and message.strip():
            messages.append(message.strip())
    return '; '.join(messages)


def _ozon_channel_row(draft: OzonOfferDraft) -> dict[str, Any]:
    status = _ozon_status(draft)
    provider_synced_at = _format_datetime(draft.last_provider_sync_at)
    provider_sku = draft.provider_sku
    external_url = (
        f'https://www.ozon.ru/product/{provider_sku}/'
        if provider_sku is not None
        else ''
    )
    return {
        'id': draft.pk,
        'resource_id': draft.pk,
        'channel_id': f'{RESOURCE_OZON_OFFER}:{draft.pk}',
        'resource_kind': RESOURCE_OZON_OFFER,
        'status': status,
        'status_display': _ozon_status_display(status),
        'status_explanation': _ozon_status_explanation(draft, status),
        'delivery_stage': 'unavailable',
        'provider_submission_started': draft.publication_status != 'local_draft',
        'lifecycle_actions_blocked': True,
        'can_check_avito_status': False,
        'can_check_provider_status': False,
        'delivery_retry_at': None,
        'delivery_retry_reason': '',
        'can_publish': False,
        'rejection_ready_to_retry': False,
        'product_id': draft.product_id,
        'product_article': draft.product.article,
        'product_name': draft.product.name,
        'product_brand': draft.product.brand,
        'account_id': draft.account_id,
        'account_name': draft.account.name,
        'marketplace': MarketplaceAccount.MARKETPLACE_OZON,
        'marketplace_label': 'Ozon',
        'provider_capabilities': provider_capabilities(
            MarketplaceAccount.MARKETPLACE_OZON,
        ).public_contract(),
        'title': draft.product.title_ai or draft.product.name,
        'price_on_listing': _format_price(_ozon_price(draft)),
        'external_id': draft.offer_id,
        'external_url': external_url,
        'ad_type': '',
        'placement_address': None,
        'address_override': '',
        'seller_address_id_override': '',
        'manager_name_override': '',
        'contact_phone_override': '',
        'bulk_placement_address': None,
        'bulk_address': '',
        'bulk_seller_address_id': '',
        'bulk_manager_name': '',
        'bulk_contact_phone': '',
        'rejection_reason': _ozon_error_text(draft.provider_errors),
        'retry_count': 0,
        'published_at': provider_synced_at if status == Listing.STATUS_ACTIVE else None,
        'last_sync_at': provider_synced_at,
        'remote_status': draft.provider_status or draft.moderation_status or None,
        'remote_status_checked_at': provider_synced_at,
        'next_status_check_at': None,
        'provider_sku': provider_sku,
        'provider_product_id': draft.provider_product_id,
        'created_at': _format_datetime(draft.created_at),
    }


def hydrate_channel_rows(
    tenant,
    keys: Iterable[Mapping[str, Any]],
    *,
    expected_status: str = '',
) -> list[dict[str, Any]]:
    """Hydrate an already bounded page with at most two resource queries."""
    ordered_keys = list(keys)
    listing_ids = [
        key['resource_id'] for key in ordered_keys
        if key['resource_kind'] == RESOURCE_LISTING
    ]
    ozon_ids = [
        key['resource_id'] for key in ordered_keys
        if key['resource_kind'] == RESOURCE_OZON_OFFER
    ]

    listing_rows: dict[int, dict[str, Any]] = {}
    if listing_ids:
        listings = list(
            Listing.objects.filter(
                pk__in=listing_ids,
                tenant=tenant,
                product__tenant=tenant,
                account__tenant=tenant,
                account__is_active=True,
                account__deleted_at__isnull=True,
                account__marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            ).exclude(status=Listing.STATUS_DELETED).select_related(
                'product', 'account', 'feed_run',
            ),
        )
        serialized = ListingSerializer(listings, many=True).data
        for listing, row in zip(listings, serialized, strict=True):
            normalized = dict(row)
            normalized.update({
                'resource_id': listing.pk,
                'channel_id': f'{RESOURCE_LISTING}:{listing.pk}',
                'resource_kind': RESOURCE_LISTING,
                'provider_sku': None,
                'provider_product_id': None,
            })
            listing_rows[listing.pk] = normalized

    ozon_rows: dict[int, dict[str, Any]] = {}
    if ozon_ids:
        drafts = OzonOfferDraft.objects.filter(
            pk__in=ozon_ids,
            tenant=tenant,
            product__tenant=tenant,
            account__tenant=tenant,
            account__is_active=True,
            account__deleted_at__isnull=True,
            account__marketplace=MarketplaceAccount.MARKETPLACE_OZON,
        ).exclude(publication_status='local_draft').select_related('product', 'account')
        ozon_rows = {draft.pk: _ozon_channel_row(draft) for draft in drafts}

    rows: list[dict[str, Any]] = []
    for key in ordered_keys:
        source = listing_rows if key['resource_kind'] == RESOURCE_LISTING else ozon_rows
        row = source.get(key['resource_id'])
        # Keys and hydration are separate READ COMMITTED statements. If a
        # provider transition races a filtered request, prefer a temporarily
        # shorter page over returning a row that violates the requested status.
        if row is not None and (
            not expected_status or row['status'] == expected_status
        ):
            rows.append(row)
    return rows
