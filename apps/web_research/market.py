from decimal import Decimal

from django.utils.timezone import now

from apps.products.models import (
    ProductCatalogClassification,
    ProductParseJob,
    ReviewStatus,
)
from apps.products.services import ProductEnrichmentService
from apps.web_research.models import CompetitorOffer, WebResearchRun
from apps.web_research.search_context import get_tenant_research_settings
from apps.web_research.serializers import CompetitorOfferSerializer, WebResearchRunSerializer


CATALOG_SOURCES = ('tachka', 'rossko', 'euroauto')


def _money(value):
    return str(value.quantize(Decimal('0.01'))) if value is not None else None


def _difference(subject: Decimal | None, reference: Decimal | None):
    """Return how much ``subject`` is above or below ``reference``."""
    if subject is None or reference is None or reference <= 0:
        return None
    amount = subject - reference
    return {
        'amount': _money(amount),
        'percent': str((amount / reference * Decimal('100')).quantize(Decimal('0.1'))),
        'direction': 'above' if amount > 0 else 'below' if amount < 0 else 'equal',
    }


def auto_parts_catalog_applicable(product) -> bool:
    """Resolve auto-parts UI eligibility without classifying or mutating the product."""
    category = product.catalog_category
    if category is not None:
        root_domain = category.root_domain
        if root_domain is not None:
            return bool(root_domain.supports_auto_parts_enrichment)
        if category.domain != category.Domain.UNKNOWN:
            return category.domain == category.Domain.AUTO_PARTS

    try:
        classification = product.catalog_classification
    except ProductCatalogClassification.DoesNotExist:
        classification = None
    if classification is not None:
        if classification.review_status == ReviewStatus.REJECTED:
            return False
        return (
            classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
            and classification.confidence >= 0.7
            and (
                classification.review_status == ReviewStatus.APPROVED
                or not classification.needs_review
            )
        )

    if ProductEnrichmentService.tenant_requires_product_auto_parts_check(product.tenant):
        return False
    return ProductEnrichmentService.tenant_supports_auto_parts_enrichment(product.tenant)


def catalog_offers(product, *, reference_price: Decimal | None = None) -> list[dict]:
    latest = {}
    jobs = ProductParseJob.objects.filter(
        tenant=product.tenant,
        product=product,
        source_id__in=CATALOG_SOURCES,
    ).order_by('source_id', '-created_at')
    for job in jobs:
        if job.source_id not in latest:
            latest[job.source_id] = job
    result = []
    for source_id in CATALOG_SOURCES:
        latest_job = latest.get(source_id)
        result.append({
            'source_id': source_id,
            'source_label': {'tachka': 'Tachka', 'rossko': 'Rossko', 'euroauto': 'Euroauto'}[source_id],
            'status': latest_job.status if latest_job else 'not_checked',
            'status_label': (
                latest_job.get_status_display() if latest_job else 'Ещё не проверялся'
            ),
            'price': _money(latest_job.source_price) if latest_job else None,
            'currency': latest_job.source_currency if latest_job else 'RUB',
            'price_is_from': latest_job.source_price_is_from if latest_job else False,
            'availability': latest_job.source_availability if latest_job else 'unknown',
            'availability_label': (
                latest_job.get_source_availability_display()
                if latest_job else 'Наличие не указано'
            ),
            'availability_text': latest_job.source_availability_text if latest_job else '',
            'quantity': latest_job.source_quantity if latest_job else None,
            'checked_at': (
                latest_job.finished_at or latest_job.updated_at
            ) if latest_job else None,
            'url': latest_job.source_url if latest_job else '',
            'difference_from_listing': _difference(
                latest_job.source_price if latest_job else None, reference_price,
            ),
            'difference_from_reference': _difference(
                latest_job.source_price if latest_job else None, reference_price,
            ),
            'difference_from_base': _difference(
                latest_job.source_price if latest_job else None, product.price,
            ),
            'message': (
                latest_job.error_message
                if latest_job and latest_job.status == ProductParseJob.Status.FAILED
                else 'Товар не найден'
                if latest_job and latest_job.status == ProductParseJob.Status.NOT_FOUND
                else ''
            ),
        })
    return result


def fresh_offer_queryset(product):
    return CompetitorOffer.objects.filter(
        tenant=product.tenant,
        product=product,
        expires_at__gt=now(),
    ).select_related('evidence', 'run')


def verified_statistics(product, *, reference_price: Decimal | None = None) -> dict:
    offers = fresh_offer_queryset(product).filter(
        review_status=CompetitorOffer.ReviewStatus.VERIFIED,
        match_type__in=[CompetitorOffer.MatchType.EXACT, CompetitorOffer.MatchType.CROSS],
        normalized_currency='RUB',
        normalized_price__isnull=False,
        is_price_from=False,
    ).exclude(availability=CompetitorOffer.Availability.OUT_OF_STOCK)
    prices = sorted(offer.normalized_price for offer in offers)
    median = None
    if prices:
        midpoint = len(prices) // 2
        median = (
            prices[midpoint]
            if len(prices) % 2
            else (prices[midpoint - 1] + prices[midpoint]) / Decimal('2')
        )
    sellers = offers.filter(availability=CompetitorOffer.Availability.IN_STOCK).values(
        'domain', 'seller_name',
    ).distinct().count()
    return {
        'minimum': _money(prices[0]) if prices else None,
        'median': _money(median),
        'maximum': _money(prices[-1]) if prices else None,
        'verified_offer_count': len(prices),
        'available_seller_count': sellers,
        'listing_vs_median': _difference(reference_price, median),
        'listing_vs_base': _difference(reference_price, product.price),
        'reference_vs_median': _difference(reference_price, median),
        'reference_vs_base': _difference(reference_price, product.price),
        'median_vs_base': _difference(median, product.price),
    }


def product_market_comparison(
    product,
    *,
    reference_price: Decimal | None = None,
    listing_id: int | None = None,
) -> dict:
    """Build one product-owned market snapshot for any publication channel."""
    reference_price = (
        Decimal(reference_price) if reference_price is not None else Decimal(product.price)
    )
    settings = get_tenant_research_settings(product.tenant)
    offers = fresh_offer_queryset(product).order_by('normalized_price', '-captured_at')
    active_run = WebResearchRun.objects.filter(
        tenant=product.tenant,
        product=product,
        purpose__in=[WebResearchRun.Purpose.PRICING, WebResearchRun.Purpose.COMBINED],
        status__in=[WebResearchRun.Status.QUEUED, WebResearchRun.Status.RUNNING],
    ).order_by('-created_at').first()
    latest_run = WebResearchRun.objects.filter(
        tenant=product.tenant,
        product=product,
        purpose__in=[WebResearchRun.Purpose.PRICING, WebResearchRun.Purpose.COMBINED],
    ).order_by('-created_at').first()
    stale_count = CompetitorOffer.objects.filter(
        tenant=product.tenant, product=product, expires_at__lte=now(),
    ).count()
    pending_count = offers.filter(review_status=CompetitorOffer.ReviewStatus.PENDING).count()
    warnings = []
    if stale_count:
        warnings.append(f'Устаревших предложений: {stale_count}')
    if pending_count:
        warnings.append(f'Требуют проверки и не участвуют в статистике: {pending_count}')
    if latest_run and latest_run.status == WebResearchRun.Status.FAILED:
        warnings.append(latest_run.error_message or 'Ошибка провайдера интернет-поиска')
    last_checked = offers.order_by('-captured_at').values_list('captured_at', flat=True).first()
    offer_objects = list(offers[:100])
    internet_offers = CompetitorOfferSerializer(offer_objects, many=True).data
    for payload, offer in zip(internet_offers, offer_objects):
        comparable_price = (
            offer.normalized_price
            if offer.normalized_currency == 'RUB'
            else None
        )
        payload['difference_from_base'] = _difference(comparable_price, product.price)
        payload['difference_from_listing'] = _difference(
            comparable_price, reference_price,
        )
        payload['difference_from_reference'] = _difference(
            comparable_price, reference_price,
        )

    catalog_applicable = auto_parts_catalog_applicable(product)

    return {
        'listing_id': listing_id,
        'product_id': product.pk,
        'base_price': _money(product.price),
        # Keep the legacy response key so existing clients remain compatible.
        # In product-scoped consumers this is the selected channel's reference price.
        'listing_price': _money(reference_price),
        'reference_price': _money(reference_price),
        'catalog_offers_applicable': catalog_applicable,
        'catalog_offers': (
            catalog_offers(product, reference_price=reference_price)
            if catalog_applicable
            else []
        ),
        'internet_offers': internet_offers,
        'statistics': verified_statistics(product, reference_price=reference_price),
        'region': {
            'preset': settings.region_preset,
            'label': settings.get_region_preset_display(),
            'country_codes': settings.country_codes,
        },
        'freshness': {
            'last_checked_at': last_checked,
            'ttl_hours': settings.price_ttl_hours,
            'fresh_offer_count': offers.count(),
            'stale_offer_count': stale_count,
        },
        'active_run': WebResearchRunSerializer(active_run).data if active_run else None,
        'latest_run': WebResearchRunSerializer(latest_run).data if latest_run else None,
        'warnings': warnings,
    }


def listing_market_comparison(listing) -> dict:
    """Backward-compatible listing projection over product market data."""
    return product_market_comparison(
        listing.product,
        reference_price=listing.price_on_listing,
        listing_id=listing.pk,
    )
