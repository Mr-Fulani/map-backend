"""Deterministic metadata gate before downloading untrusted search results."""

import re
from urllib.parse import unquote, urlparse

from apps.image_search.sources.base import ImageCandidate
from apps.image_search.services.query_builder import _is_unreliable_article


_OBVIOUS_SERVICE_MARKERS = (
    'placeholder', 'brandlogos/', 'getclicky.com', 'tracking', 'sprite',
    'favicon', 'logo.', '/logo/', 'pixel.', 'spacer.', '/banner/',
)
_OBVIOUS_NON_PRODUCT_TITLES = (
    'логотип', 'logo', 'иконка', 'icon', 'схема', 'diagram', 'manual pdf',
)
_POSITION_GROUPS = (
    ({'левый', 'левая', 'левое', 'left'}, {'правый', 'правая', 'правое', 'right'}),
    ({'передний', 'передняя', 'переднее', 'front'}, {'задний', 'задняя', 'заднее', 'rear'}),
    ({'внешний', 'внешняя', 'внешнее', 'outer'}, {'внутренний', 'внутренняя', 'внутреннее', 'inner'}),
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
    position_conflict = _has_position_conflict(product, title)
    if position_conflict:
        return False, ['conflicting_side_or_position'], 0.0

    fitment_match = _matches_trusted_fitment(product, title_words)
    name_identity_match = _matches_name_identity(product, title_words)
    unreliable_identity = not article or _is_unreliable_article(
        getattr(product, 'article', ''), getattr(product, 'brand', ''),
    )
    textual_context_match = bool(
        unreliable_identity
        and len(expected_words) >= 3
        and word_overlap >= min(4, len(expected_words))
    )
    contextual_match = fitment_match or name_identity_match or textual_context_match

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
    if contextual_match:
        relevance += 0.35
        reasons.append('vehicle_and_part_context_match')

    if any(marker in title for marker in _OBVIOUS_NON_PRODUCT_TITLES) and not article_match:
        return False, ['non_product_title'], min(relevance, 1.0)

    # General web search (tier 3+) must carry evidence that the result belongs
    # to this product. Exact catalogue adapters (tier 1/2) validate identity themselves.
    if (
        candidate.tier >= 3
        and not article_match
        and not (brand_match and word_overlap)
        and not contextual_match
    ):
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


def _has_position_conflict(product, title: str) -> bool:
    expected = set(re.findall(
        r'[a-zа-яё]+',
        f"{getattr(product, 'name', '')} {getattr(product, 'category_1c', '')}".lower(),
    ))
    actual = set(re.findall(r'[a-zа-яё]+', title.lower()))
    for left, right in _POSITION_GROUPS:
        if (expected & left and actual & right) or (expected & right and actual & left):
            return True
    return False


def _matches_trusted_fitment(product, title_words: set[str]) -> bool:
    """Проверяет марку+модель из доверенной применяемости и тип детали."""
    contexts = getattr(product, '_image_search_fitment_contexts', None)
    if contexts is None:
        contexts = []
        manager = getattr(product, 'fitments', None)
        if manager is not None and hasattr(manager, 'all'):
            from apps.products.source_policy import should_auto_apply_fitment

            try:
                fitments = list(manager.all()[:10])
            except (TypeError, AttributeError):
                fitments = []
            for fitment in fitments:
                if not should_auto_apply_fitment(fitment):
                    continue
                make_words = _identity_words(getattr(fitment, 'make', ''))
                model_words = _identity_words(getattr(fitment, 'model', ''))
                generation_words = _identity_words(getattr(fitment, 'generation', ''))
                if make_words and model_words:
                    contexts.append((make_words, model_words, generation_words))
        product._image_search_fitment_contexts = contexts

    part_words = _expected_words(product)
    for make_words, model_words, generation_words in contexts:
        vehicle_words = make_words | model_words | generation_words
        relevant_part_words = part_words - vehicle_words
        if (
            make_words <= title_words
            and model_words <= title_words
            and bool(relevant_part_words & title_words)
        ):
            return True
    return False


def _identity_words(value: object) -> set[str]:
    return {
        token for token in re.findall(r'[0-9a-zа-яё]+', str(value or '').lower())
        if len(token) >= 2
    }


def _matches_name_identity(product, title_words: set[str]) -> bool:
    """Находит в названии пары собственных имён вроде ``Kia Optima``."""
    phrases = getattr(product, '_image_search_name_identity_phrases', None)
    if phrases is None:
        phrases = []
        run: list[str] = []
        for token in re.findall(r'[0-9A-Za-zА-Яа-яЁё]+', getattr(product, 'name', '')):
            if len(token) >= 2 and token[0].isupper():
                run.append(token.lower())
                continue
            if len(run) >= 2:
                phrases.append(set(run))
            run = []
        if len(run) >= 2:
            phrases.append(set(run))
        product._image_search_name_identity_phrases = phrases

    part_words = _expected_words(product)
    for phrase in phrases:
        if phrase <= title_words and bool((part_words - phrase) & title_words):
            return True
    return False
