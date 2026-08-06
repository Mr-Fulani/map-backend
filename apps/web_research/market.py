from decimal import Decimal

from django.utils.timezone import now

from apps.products.models import ProductParseJob
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


def catalog_offers(product, *, listing_price: Decimal | None = None) -> list[dict]:
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
        job = latest.get(source_id)
        result.append({
            'source_id': source_id,
            'source_label': {'tachka': 'Tachka', 'rossko': 'Rossko', 'euroauto': 'Euroauto'}[source_id],
            'status': job.status if job else 'not_checked',
            'status_label': job.get_status_display() if job else 'Ещё не проверялся',
            'price': _money(job.source_price) if job else None,
            'currency': job.source_currency if job else 'RUB',
            'price_is_from': job.source_price_is_from if job else False,
            'availability': job.source_availability if job else 'unknown',
            'availability_label': job.get_source_availability_display() if job else 'Наличие не указано',
            'availability_text': job.source_availability_text if job else '',
            'quantity': job.source_quantity if job else None,
            'checked_at': (job.finished_at or job.updated_at) if job else None,
            'url': job.source_url if job else '',
            'difference_from_listing': _difference(
                job.source_price if job else None, listing_price,
            ),
            'difference_from_base': _difference(
                job.source_price if job else None, product.price,
            ),
            'message': (
                job.error_message if job and job.status == ProductParseJob.Status.FAILED
                else 'Товар не найден' if job and job.status == ProductParseJob.Status.NOT_FOUND
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


def verified_statistics(product, *, listing_price: Decimal | None = None) -> dict:
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
        'listing_vs_median': _difference(listing_price, median),
        'listing_vs_base': _difference(listing_price, product.price),
        'median_vs_base': _difference(median, product.price),
    }


def listing_market_comparison(listing) -> dict:
    product = listing.product
    settings = get_tenant_research_settings(listing.tenant)
    offers = fresh_offer_queryset(product).order_by('normalized_price', '-captured_at')
    active_run = WebResearchRun.objects.filter(
        tenant=listing.tenant,
        product=product,
        purpose__in=[WebResearchRun.Purpose.PRICING, WebResearchRun.Purpose.COMBINED],
        status__in=[WebResearchRun.Status.QUEUED, WebResearchRun.Status.RUNNING],
    ).order_by('-created_at').first()
    latest_run = WebResearchRun.objects.filter(
        tenant=listing.tenant,
        product=product,
        purpose__in=[WebResearchRun.Purpose.PRICING, WebResearchRun.Purpose.COMBINED],
    ).order_by('-created_at').first()
    stale_count = CompetitorOffer.objects.filter(
        tenant=listing.tenant, product=product, expires_at__lte=now(),
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
            comparable_price, listing.price_on_listing,
        )

    return {
        'listing_id': listing.pk,
        'product_id': product.pk,
        'base_price': _money(product.price),
        'listing_price': _money(listing.price_on_listing),
        'catalog_offers': catalog_offers(product, listing_price=listing.price_on_listing),
        'internet_offers': internet_offers,
        'statistics': verified_statistics(product, listing_price=listing.price_on_listing),
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
