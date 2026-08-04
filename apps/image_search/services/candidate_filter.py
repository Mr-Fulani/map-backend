"""Deterministic metadata gate before downloading untrusted search results."""

import re
from urllib.parse import unquote, urlparse

from apps.image_search.sources.base import ImageCandidate


_OBVIOUS_SERVICE_MARKERS = (
    'placeholder', 'brandlogos/', 'getclicky.com', 'tracking', 'sprite',
    'favicon', 'logo.', '/logo/', 'pixel.', 'spacer.', '/banner/',
)
_OBVIOUS_NON_PRODUCT_TITLES = (
    'логотип', 'logo', 'иконка', 'icon', 'схема', 'diagram', 'manual pdf',
)


def normalize_search_identity(value: object) -> str:
    return re.sub(r'[^0-9a-zа-яё]+', '', str(value or '').lower())


def candidate_metadata_assessment(
    product,
    candidate: ImageCandidate,
) -> tuple[bool, list[str], float]:
    """Return (allowed, reason codes, relevance score) without image inference."""
    url = unquote(candidate.url or '').lower()
    title = str(candidate.raw_meta.get('title', '') or '').lower()
    combined = f'{title} {url}'
    parsed = urlparse(candidate.url or '')
    path = parsed.path.lower()
    reasons: list[str] = []

    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return False, ['invalid_url'], 0.0
    if path.endswith(('.gif', '.svg')):
        return False, ['unsupported_search_image_type'], 0.0
    if any(marker in combined for marker in _OBVIOUS_SERVICE_MARKERS):
        return False, ['service_image'], 0.0

    article = normalize_search_identity(getattr(product, 'article', ''))
    brand = normalize_search_identity(getattr(product, 'brand', ''))
    normalized_combined = normalize_search_identity(combined)
    identity_tokens = {
        normalize_search_identity(token)
        for token in re.findall(r'[0-9a-zа-яё]+', combined)
    }
    article_match = bool(
        article
        and (
            article in identity_tokens
            or (len(article) >= 5 and article in normalized_combined)
        )
    )
    brand_match = bool(
        brand
        and (
            brand in identity_tokens
            or (len(brand) >= 4 and brand in normalized_combined)
        )
    )

    expected_words = _expected_words(product)
    title_words = set(re.findall(r'[0-9a-zа-яё]{3,}', title))
    word_overlap = len(expected_words & title_words)

    relevance = 0.0
    if article_match:
        relevance += 0.55
        reasons.append('article_match')
    if brand_match:
        relevance += 0.20
        reasons.append('brand_match')
    if word_overlap:
        relevance += min(0.25, word_overlap * 0.08)
        reasons.append('name_or_category_match')

    if any(marker in title for marker in _OBVIOUS_NON_PRODUCT_TITLES) and not article_match:
        return False, ['non_product_title'], min(relevance, 1.0)

    # General web search (tier 3+) must carry evidence that the result belongs
    # to this product. Exact catalogue adapters (tier 1/2) validate identity themselves.
    if candidate.tier >= 3 and not article_match and not (brand_match and word_overlap):
        return False, ['insufficient_product_identity'], min(relevance, 1.0)

    return True, reasons or ['catalog_source'], min(relevance, 1.0)


def _expected_words(product) -> set[str]:
    values = [
        getattr(product, 'name', ''),
        getattr(product, 'category_1c', ''),
    ]
    category = getattr(product, 'catalog_category', None)
    if category is not None:
        values.append(getattr(category, 'name', ''))
    stop_words = {
        'для', 'авто', 'автомобиль', 'автозапчасть', 'запчасть', 'новый',
        'комплект', 'левый', 'правый', 'передний', 'задний',
    }
    words = set(re.findall(r'[0-9a-zа-яё]{3,}', ' '.join(values).lower()))
    return words - stop_words
