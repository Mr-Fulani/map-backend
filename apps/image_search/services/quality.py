"""Оценка качества изображений-кандидатов.

Скор [0.0–1.0] вычисляется до скачивания на основе:
- Приоритета источника (tier): меньше tier — выше доверие
- Уверенности поискового запроса (confidence): HIGH/MEDIUM/LOW/VERY_LOW
- Разрешения (если известно из метаданных источника)
"""

from apps.image_search.sources.base import ImageCandidate

# Вклад уверенности запроса в итоговый скор
_CONFIDENCE_SCORES: dict[str, float] = {
    'HIGH': 0.50,
    'MEDIUM': 0.30,
    'LOW': 0.15,
    'VERY_LOW': 0.05,
}

# Вклад tier источника в итоговый скор (tier1 = автозапчастный сайт, tier4 = DDG)
_TIER_SCORES: dict[int, float] = {
    1: 0.40,
    2: 0.30,
    3: 0.20,
    4: 0.10,
}


def score(candidate: ImageCandidate) -> float:
    """Вычисляет качество кандидата — значение от 0.0 до 1.0.

    Используется для:
    - фильтрации (MIN_QUALITY_SCORE из настроек)
    - определения статуса ProductImage (auto_approved vs needs_review)

    Args:
        candidate: Кандидат-изображение с заполненным raw_meta['confidence'].

    Returns:
        Скор от 0.0 до 1.0.
    """
    confidence = candidate.raw_meta.get('confidence', '')
    s = _CONFIDENCE_SCORES.get(confidence, 0.05)
    s += _TIER_SCORES.get(candidate.tier, 0.05)

    # Бонус за разрешение если известно из метаданных источника
    if candidate.width and candidate.height:
        min_dim = min(candidate.width, candidate.height)
        if min_dim >= 600:
            s += 0.20
        elif min_dim >= 300:
            s += 0.10

    # Metadata relevance is calculated against the concrete Product before download.
    # It cannot prove visual identity, but rewards exact article/brand/category evidence.
    relevance = float(candidate.raw_meta.get('metadata_relevance', 0) or 0)
    s += min(max(relevance, 0.0), 1.0) * 0.10

    return min(s, 1.0)
