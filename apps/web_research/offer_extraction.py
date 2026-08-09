import hashlib
import json
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit, urlunsplit

from django.utils.timezone import now
from lxml import html

from apps.products.enrichment import normalize_part_code
from apps.web_research.models import CompetitorOffer
from apps.web_research.search_context import infer_country_code, normalize_country_codes


PRICE_RE = re.compile(
    r'(?P<from>от\s+)?(?P<price>\d[\d\s\u00a0]{1,12}(?:[.,]\d{1,2})?)\s*'
    r'(?P<currency>₽|руб(?:\.|лей)?|RUB|USD|EUR|BYN|KZT|₸|€|\$)',
    re.IGNORECASE,
)
CURRENCY_MAP = {
    '₽': 'RUB', 'РУБ': 'RUB', 'РУБ.': 'RUB', 'РУБЛЕЙ': 'RUB', 'RUB': 'RUB',
    '$': 'USD', 'USD': 'USD', '€': 'EUR', 'EUR': 'EUR', 'BYN': 'BYN',
    '₸': 'KZT', 'KZT': 'KZT',
}


def _decimal(value) -> Decimal | None:
    cleaned = re.sub(r'[\s\u00a0]', '', str(value or '')).replace(',', '.')
    cleaned = re.sub(r'[^\d.]', '', cleaned)
    try:
        result = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if result <= 0 or result > Decimal('999999999999.99'):
        return None
    return result.quantize(Decimal('0.01'))


def _availability(value: str) -> tuple[str, str]:
    raw = str(value or '').strip()
    lowered = raw.casefold()
    if any(token in lowered for token in ['instock', 'in stock', 'в наличии', 'есть в наличии']):
        return CompetitorOffer.Availability.IN_STOCK, raw
    if any(token in lowered for token in ['preorder', 'pre-order', 'под заказ', 'предзаказ']):
        return CompetitorOffer.Availability.PREORDER, raw
    if any(token in lowered for token in ['outofstock', 'out of stock', 'нет в наличии', 'распродан']):
        return CompetitorOffer.Availability.OUT_OF_STOCK, raw
    return CompetitorOffer.Availability.UNKNOWN, raw


def _condition(value: str) -> str:
    lowered = str(value or '').casefold()
    if any(token in lowered for token in ['usedcondition', 'б/у', 'бывш', 'used']):
        return CompetitorOffer.Condition.USED
    if any(token in lowered for token in ['newcondition', 'новый', 'новая', 'new']):
        return CompetitorOffer.Condition.NEW
    return CompetitorOffer.Condition.UNKNOWN


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _types(item: dict) -> set[str]:
    value = item.get('@type') or item.get('type') or []
    if isinstance(value, str):
        value = [value]
    return {str(token).casefold() for token in value}


def _json_ld_candidates(content: str) -> list[dict]:
    if not content or '<' not in content:
        return []
    try:
        tree = html.fromstring(content)
    except (ValueError, TypeError):
        return []
    candidates = []
    json_ld_xpath = (
        '//script[contains('
        'translate(@type, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), '
        '"ld+json")]'
    )
    for node in tree.xpath(json_ld_xpath):
        try:
            payload = json.loads(node.text or '')
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk_json(payload):
            if 'product' not in _types(item):
                continue
            offers = item.get('offers') or []
            if isinstance(offers, dict):
                offers = [offers]
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                seller = offer.get('seller') or item.get('brand') or ''
                if isinstance(seller, dict):
                    seller = seller.get('name') or ''
                brand = item.get('brand') or ''
                if isinstance(brand, dict):
                    brand = brand.get('name') or ''
                candidates.append({
                    'source': 'json_ld',
                    'title': item.get('name') or '',
                    'article': item.get('sku') or item.get('mpn') or item.get('productID') or '',
                    'brand': brand,
                    'seller_name': seller,
                    'price': offer.get('price') or offer.get('lowPrice'),
                    'currency': offer.get('priceCurrency') or '',
                    'availability': offer.get('availability') or '',
                    'condition': offer.get('itemCondition') or '',
                    'url': offer.get('url') or item.get('url') or '',
                })
    return candidates


def _html_candidates(content: str) -> list[dict]:
    if not content or '<' not in content:
        return []
    try:
        tree = html.fromstring(content)
    except (ValueError, TypeError):
        return []

    def first(xpath: str) -> str:
        values = tree.xpath(xpath)
        if not values:
            return ''
        value = values[0]
        if hasattr(value, 'get'):
            value = value.get('content') or value.get('value') or value.text_content()
        return str(value or '').strip()

    price = first('//*[@itemprop="price"]/@content | //*[@itemprop="price"]/@value | //*[@itemprop="price"]/text()')
    currency = first('//*[@itemprop="priceCurrency"]/@content | //*[@itemprop="priceCurrency"]/text()')
    source = 'microdata'
    if not price:
        price = first(
            '//meta[@property="product:price:amount"]/@content | '
            '//meta[@property="og:price:amount"]/@content',
        )
        currency = currency or first(
            '//meta[@property="product:price:currency"]/@content | '
            '//meta[@property="og:price:currency"]/@content',
        )
        source = 'opengraph'
    if not price:
        return []
    return [{
        'source': source,
        'title': first('//*[@itemprop="name"]/text() | //meta[@property="og:title"]/@content | //title/text()'),
        'article': first(
            '//*[@itemprop="sku"]/@content | //*[@itemprop="sku"]/text() | '
            '//*[@itemprop="mpn"]/@content | //*[@itemprop="mpn"]/text()',
        ),
        'brand': first('//*[@itemprop="brand"]/@content | //*[@itemprop="brand"]//text()'),
        'seller_name': first('//*[@itemprop="seller"]/@content | //*[@itemprop="seller"]//text()'),
        'price': price,
        'currency': currency,
        'availability': first(
            '//*[@itemprop="availability"]/@href | '
            '//*[@itemprop="availability"]/@content | '
            '//*[@itemprop="availability"]//text()',
        ),
        'condition': first('//*[@itemprop="itemCondition"]/@href | //*[@itemprop="itemCondition"]/@content'),
        'url': '',
    }]


def _text_candidate(text: str) -> list[dict]:
    match = PRICE_RE.search(text or '')
    if not match:
        return []
    return [{
        'source': 'html_fallback',
        'title': '', 'article': '', 'brand': '', 'seller_name': '',
        'price': match.group('price'), 'currency': match.group('currency'),
        'availability': text, 'condition': text, 'url': '',
        'is_price_from': bool(match.group('from')),
    }]


def extract_offer_candidates(evidence) -> list[dict]:
    content = evidence.raw_content or ''
    candidates = _json_ld_candidates(content)
    if not candidates:
        candidates = _html_candidates(content)
    if not candidates:
        candidates = _text_candidate(' '.join([evidence.title, evidence.snippet, content[:10000]]))
    return candidates


def _product_codes(product) -> tuple[set[str], str]:
    direct = {normalize_part_code(product.article)} if normalize_part_code(product.article) else set()
    related = {
        normalize_part_code(value)
        for value in [*(product.oem_numbers or []), *(product.cross_numbers or [])]
        if normalize_part_code(value)
    }
    related.update(
        value for value in product.cross_codes.values_list('normalized_code', flat=True) if value
    )
    return direct | related, normalize_part_code(product.brand)


def _match_product(product, candidate: dict, evidence) -> tuple[str, float, str, list[str]]:
    codes, product_brand = _product_codes(product)
    haystack = ' '.join([
        str(candidate.get('article') or ''), str(candidate.get('title') or ''),
        evidence.title, evidence.snippet,
    ])
    normalized_text = normalize_part_code(haystack)
    matched = next((code for code in sorted(codes, key=len, reverse=True) if code in normalized_text), '')
    candidate_brand = normalize_part_code(candidate.get('brand') or haystack)
    brand_matches = bool(product_brand and product_brand in candidate_brand)
    reasons = []
    if matched:
        reasons.append(f'Совпал артикул/OEM {matched}')
    if brand_matches:
        reasons.append('Совпал бренд')
    direct = normalize_part_code(product.article)
    if matched and matched == direct and (brand_matches or not product_brand):
        return CompetitorOffer.MatchType.EXACT, 0.95, matched, reasons
    if matched and matched != direct and (brand_matches or not product_brand):
        return CompetitorOffer.MatchType.CROSS, 0.88, matched, reasons
    if matched:
        reasons.append('Бренд не подтверждён')
        return CompetitorOffer.MatchType.REVIEW, 0.68, matched, reasons
    return CompetitorOffer.MatchType.REVIEW, 0.35, '', ['Артикул/OEM не подтверждён']


def _canonical_url(value: str) -> str:
    split = urlsplit(value)
    return urlunsplit((split.scheme.lower(), split.netloc.lower(), split.path.rstrip('/'), '', ''))


def save_deterministic_offers(run, evidence_items, *, ttl_hours: int) -> list[CompetitorOffer]:
    allowed_countries = set(normalize_country_codes(run.settings_snapshot.get('country_codes')))
    if run.settings_snapshot.get('region_preset') == 'russia':
        allowed_countries = {'RU'}
    include_used = bool(run.settings_snapshot.get('include_used'))
    include_preorder = bool(run.settings_snapshot.get('include_preorder', True))
    expires_at = now() + timedelta(hours=max(1, ttl_hours))
    saved = []
    for evidence in evidence_items:
        for candidate in extract_offer_candidates(evidence):
            price = _decimal(candidate.get('price'))
            currency_raw = str(candidate.get('currency') or '').strip().upper()
            currency = CURRENCY_MAP.get(currency_raw, currency_raw[:3])
            if not price or len(currency) != 3:
                continue
            availability, availability_text = _availability(candidate.get('availability') or '')
            condition = _condition(candidate.get('condition') or '')
            if condition == CompetitorOffer.Condition.USED and not include_used:
                continue
            if availability == CompetitorOffer.Availability.PREORDER and not include_preorder:
                continue
            match_type, confidence, matched_code, reasons = _match_product(
                run.product, candidate, evidence,
            )
            text = ' '.join([evidence.title, evidence.snippet, str(candidate.get('title') or '')])
            country_code = infer_country_code(evidence.url, text)
            region_ok = not allowed_countries or (country_code and country_code in allowed_countries)
            verified = (
                match_type in {CompetitorOffer.MatchType.EXACT, CompetitorOffer.MatchType.CROSS}
                and region_ok
                and not candidate.get('is_price_from')
            )
            if not country_code:
                reasons.append('Страна продавца не подтверждена')
            elif not region_ok:
                reasons.append('Предложение за пределами выбранного региона')
            url = str(candidate.get('url') or evidence.url)[:2000]
            seller = str(candidate.get('seller_name') or evidence.domain)[:255]
            dedupe_source = '|'.join([_canonical_url(url), seller.casefold(), matched_code])
            dedupe_key = hashlib.sha256(dedupe_source.encode()).hexdigest()
            offer, _ = CompetitorOffer.objects.update_or_create(
                run=run,
                dedupe_key=dedupe_key,
                defaults={
                    'tenant': run.tenant,
                    'product': run.product,
                    'evidence': evidence,
                    'provider_id': evidence.provider_id,
                    'seller_name': seller,
                    'domain': evidence.domain,
                    'url': url,
                    'country_code': country_code,
                    'region': '',
                    'title': str(candidate.get('title') or evidence.title)[:500],
                    'article': str(candidate.get('article') or '')[:100],
                    'matched_code': matched_code[:100],
                    'match_type': match_type,
                    'match_confidence': confidence,
                    'match_reasons': reasons,
                    'price': price,
                    'currency': currency,
                    'normalized_price': price if currency == 'RUB' else None,
                    'normalized_currency': 'RUB',
                    'is_price_from': bool(candidate.get('is_price_from')),
                    'availability': availability,
                    'availability_text': availability_text[:255],
                    'condition': condition,
                    'delivery_text': '',
                    'review_status': (
                        CompetitorOffer.ReviewStatus.VERIFIED
                        if verified else CompetitorOffer.ReviewStatus.PENDING
                    ),
                    'captured_at': now(),
                    'expires_at': expires_at,
                },
            )
            saved.append(offer)
    return saved
