"""Deterministic product-identity gate before downloading search results."""

import re
from urllib.parse import unquote, urlparse

from apps.image_search.services.query_builder import (
    _is_generic_brand, _is_unreliable_article, _trusted_cross_codes,
)
from apps.image_search.sources.base import ImageCandidate


_OBVIOUS_SERVICE_MARKERS = (
    'placeholder', 'brandlogos/', 'getclicky.com', 'tracking', 'sprite',
    'favicon', 'logo.', '/logo/', 'pixel.', 'spacer.', '/banner/',
)
_OBVIOUS_NON_PRODUCT_TITLES = (
    'логотип', ' logo ', 'иконка', ' icon ', 'схема', 'diagram', 'manual pdf',
    'мужчина', 'женщина', 'человек', 'portrait', 'selfie', 'model posing',
)
_DIRECTIONS = {
    'left': {'левый', 'левая', 'левое', 'левые', 'left', 'lh'},
    'right': {'правый', 'правая', 'правое', 'правые', 'right', 'rh'},
    'front': {'передний', 'передняя', 'переднее', 'front'},
    'rear': {'задний', 'задняя', 'заднее', 'rear'},
    'inner': {'внутренний', 'внутренняя', 'inner'},
    'outer': {'внешний', 'внешняя', 'outer'},
}
_CONTRADICTS = {
    'left': 'right', 'right': 'left', 'front': 'rear', 'rear': 'front',
    'inner': 'outer', 'outer': 'inner',
}
_PART_TERMS = {
    'фонарь', 'фара', 'бампер', 'крыло', 'капот', 'дверь', 'зеркало',
    'решетка', 'решётка', 'радиатор', 'фильтр', 'колодка', 'колодки',
    'диск', 'амортизатор', 'пружина', 'рычаг', 'ступица', 'подшипник',
    'стартер', 'генератор', 'насос', 'датчик', 'свеча', 'ремень',
    'прокладка', 'форсунка', 'катушка', 'суппорт', 'lamp', 'headlight',
    'taillight', 'bumper', 'fender', 'hood', 'door', 'mirror', 'filter',
    'brake', 'sensor', 'bearing', 'radiator', 'starter', 'alternator',
}


def normalize_search_identity(value: object) -> str:
    return re.sub(r'[^0-9a-zа-яё]+', '', str(value or '').lower())


def candidate_metadata_assessment(
    product,
    candidate: ImageCandidate,
) -> tuple[bool, list[str], float]:
    """Return ``(allowed, reason_codes, identity_score)`` without image inference."""
    url = unquote(candidate.url or '').lower()
    title = str(candidate.raw_meta.get('title', '') or '').lower()
    description = str(candidate.raw_meta.get('description', '') or '').lower()
    combined = f' {title} {description} {url} '
    parsed = urlparse(candidate.url or '')
    path = parsed.path.lower()

    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return False, ['invalid_url'], 0.0
    if path.endswith(('.gif', '.svg')):
        return False, ['unsupported_search_image_type'], 0.0
    if any(marker in combined for marker in _OBVIOUS_SERVICE_MARKERS):
        return False, ['service_image'], 0.0

    normalized_combined = normalize_search_identity(combined)
    identity_tokens = set(re.findall(r'[0-9a-zа-яё]+', combined))
    reasons: list[str] = []
    relevance = 0.0

    codes = [code for _, code in _trusted_cross_codes(product)]
    article = str(getattr(product, 'article', '') or '').strip()
    brand_value = str(getattr(product, 'brand', '') or '').strip()
    if article and not _is_unreliable_article(article, brand_value):
        codes.append(article)
    code_match = any(_identity_match(code, identity_tokens, normalized_combined) for code in codes)
    if code_match:
        relevance += 0.75
        reasons.append('trusted_code_match')

    brand = '' if _is_generic_brand(brand_value) else normalize_search_identity(brand_value)
    brand_match = bool(brand and _identity_match(brand, identity_tokens, normalized_combined))
    if brand_match:
        relevance += 0.12
        reasons.append('brand_match')

    expected_words = _expected_words(product)
    candidate_words = set(re.findall(r'[0-9a-zа-яё]{3,}', f'{title} {description}'))
    overlap = expected_words & candidate_words
    if overlap:
        relevance += min(0.64, len(overlap) * 0.13)
        reasons.append('product_context_match')

    contradiction = _direction_contradiction(product, candidate_words)
    if contradiction:
        return False, [contradiction], min(relevance, 1.0)
    if any(marker in combined for marker in _OBVIOUS_NON_PRODUCT_TITLES):
        return False, ['non_product_title'], min(relevance, 1.0)

    # A general search result without an exact code must describe both the
    # expected part type and enough vehicle/product context. This keeps people,
    # complete cars and unrelated parts out without rejecting a correct small photo.
    expected_parts = expected_words & _PART_TERMS
    part_match = bool(expected_parts & candidate_words)
    if part_match:
        reasons.append('part_type_match')

    if candidate.tier >= 3:
        direction_words = set().union(*_DIRECTIONS.values())
        context_words = expected_words - _PART_TERMS - direction_words - {
            'система', 'каталог', 'деталь', 'parts',
        }
        context_overlap = context_words & candidate_words
        required_context = 2 if len(context_words) >= 2 else len(context_words)
        enough_context = (
            len(overlap) >= 3
            and (part_match or not expected_parts)
            and len(context_overlap) >= required_context
        )
        if not code_match and not enough_context:
            return False, ['insufficient_product_identity'], min(relevance, 1.0)
        if relevance < 0.45:
            return False, ['insufficient_product_identity'], min(relevance, 1.0)

    return True, reasons or ['catalog_source'], min(relevance, 1.0)


def _identity_match(value: object, tokens: set[str], normalized_combined: str) -> bool:
    normalized = normalize_search_identity(value)
    return bool(
        normalized
        and (normalized in tokens or (len(normalized) >= 5 and normalized in normalized_combined))
    )


def _expected_words(product) -> set[str]:
    values = [getattr(product, 'name', ''), getattr(product, 'category_1c', '')]
    category = getattr(product, 'catalog_category', None)
    if category is not None:
        values.append(getattr(category, 'name', ''))

    manager = getattr(product, 'fitments', None)
    if manager is not None and hasattr(manager, 'filter'):
        try:
            rows = manager.exclude(review_status='rejected').values_list(
                'make', 'model', 'generation',
            )[:3]
            values.extend(' '.join(filter(None, row)) for row in rows)
        except (AttributeError, TypeError):
            pass

    stop_words = {
        'для', 'авто', 'автомобиль', 'автозапчасть', 'запчасть', 'новый',
        'комплект', 'original', 'оригинал',
    }
    words = set(re.findall(r'[0-9a-zа-яё]{3,}', ' '.join(values).lower()))
    return {word for word in words - stop_words if not word.isdigit()}


def _direction_contradiction(product, candidate_words: set[str]) -> str:
    expected_words = set(re.findall(
        r'[0-9a-zа-яё]+',
        f"{getattr(product, 'name', '')} {getattr(product, 'category_1c', '')}".lower(),
    ))
    expected_directions = {
        direction for direction, words in _DIRECTIONS.items() if expected_words & words
    }
    candidate_directions = {
        direction for direction, words in _DIRECTIONS.items() if candidate_words & words
    }
    for expected in expected_directions:
        if _CONTRADICTS[expected] in candidate_directions:
            return f'contradicting_{expected}_{_CONTRADICTS[expected]}'
    return ''
