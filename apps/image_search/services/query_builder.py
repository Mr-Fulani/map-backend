"""Формирование поисковых запросов для товара.

Использует реальные поля Product: brand, article, name, category_1c, catalog_category.
Учитывает ненадёжные артикулы (OEM-префиксы, aftermarket).
"""

import re


QUERY_BUILDER_VERSION = 'v4'

_GENERIC_BRANDS = {
    'oem', 'o.e.m', 'o.e.m.', 'no name', 'noname', 'generic', 'aftermarket',
    'аналог', 'без бренда', 'не определен', 'не определён',
}


def build_queries(product) -> list[tuple[str, str]]:
    """Формирует поисковые запросы с уровнем уверенности.

    Возвращает list[tuple[query, confidence_level]].
    confidence_level: 'HIGH', 'MEDIUM', 'LOW', 'VERY_LOW'.

    Args:
        product: экземпляр Product с полями brand, article, name, category_1c, catalog_category.

    Returns:
        Список кортежей (запрос, уверенность).
    """
    queries = []
    pn = product.article.strip() if product.article else ''
    raw_mfr = product.brand.strip() if product.brand else ''
    mfr = '' if _is_generic_brand(raw_mfr) else raw_mfr
    nom = product.name.strip() if product.name else ''

    category, subcategory = _get_category_context(product)
    # Наиболее конкретный доступный уровень категории для уточнения запросов
    cat_hint = subcategory or category

    desc_words = _extract_keywords(nom, max_words=6)
    fitments = _trusted_fitments(product)
    fitment_hint = fitments[0] if fitments else ''

    # Confirmed catalogue/internet OEM codes are more trustworthy than an
    # integration's internal SKU (for example OEM0099FONR).
    for manufacturer, cross_code in _trusted_cross_codes(product)[:2]:
        manufacturer_hint = manufacturer
        if manufacturer and manufacturer.casefold() in fitment_hint.casefold().split():
            manufacturer_hint = ''
        queries.append((
            ' '.join(filter(None, [
                manufacturer_hint if not _is_generic_brand(manufacturer_hint) else '',
                f'"{cross_code}"', fitment_hint, desc_words or cat_hint,
            ])),
            'HIGH',
        ))

    # Q1/Q2 deliberately contain identity and product context together. Search
    # providers currently execute only the first few queries, so weaker context
    # must not be deferred to the end of the list.
    if pn and not _is_unreliable_article(pn, raw_mfr):
        quoted_pn = f'"{pn}"'
        queries.append((
            ' '.join(filter(None, [mfr, quoted_pn, cat_hint or 'автозапчасть'])),
            'HIGH',
        ))
        queries.append((
            ' '.join(filter(None, [mfr, quoted_pn, desc_words, cat_hint])),
            'HIGH',
        ))

    # Q2: Очищенный артикул (без спецсимволов)
    clean_pn = re.sub(r'[^A-Za-z0-9]', '', pn)
    if clean_pn and clean_pn != pn:
        queries.append((
            ' '.join(filter(None, [mfr, clean_pn, cat_hint or desc_words])),
            'MEDIUM',
        ))

    # Q3: fallback for unreliable/missing articles and an additional diagnostic query.
    if desc_words:
        ctx = ' '.join(filter(None, [mfr, desc_words, cat_hint]))
        queries.append((ctx, 'LOW'))

    fallback = f'{nom} {cat_hint}'.strip() if cat_hint else nom
    return _deduplicate_queries(queries) or [(fallback, 'LOW')]


def _trusted_cross_codes(product) -> list[tuple[str, str]]:
    manager = getattr(product, 'cross_codes', None)
    if manager is None or not hasattr(manager, 'filter'):
        return []
    rows = list(
        manager.exclude(code='')
        .values_list('manufacturer', 'code', 'code_type')[:10]
    )
    priority = {'OEM': 0, 'Cross': 1, 'Trade': 2, 'Unknown': 3}
    rows.sort(key=lambda row: priority.get(row[2], 4))
    return [(manufacturer, code) for manufacturer, code, _ in rows[:2]]


def _trusted_fitments(product) -> list[str]:
    manager = getattr(product, 'fitments', None)
    if manager is None or not hasattr(manager, 'filter'):
        return []
    from django.db.models import Q

    rows = manager.filter(
        Q(review_status='approved') | Q(needs_review=False),
    ).exclude(review_status='rejected').values_list(
        'make', 'model', 'generation',
    )[:2]
    return [
        ' '.join(str(value or '').strip() for value in row if str(value or '').strip())
        for row in rows
    ]


def _is_generic_brand(brand: str) -> bool:
    normalized = re.sub(r'\s+', ' ', str(brand or '').strip().lower())
    return not normalized or normalized in _GENERIC_BRANDS


def _deduplicate_queries(queries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    result = []
    seen = set()
    for query, confidence in queries:
        normalized = ' '.join(query.split()).casefold()
        if normalized and normalized not in seen:
            result.append((' '.join(query.split()), confidence))
            seen.add(normalized)
    return result


def _get_category_context(product) -> tuple[str, str]:
    """Возвращает (категория, подкатегория) из данных товара.

    Приоритет: catalog_category FK → category_1c строка.
    Из FK: parent.name → category, name → subcategory.
    Из строки: разбивает по / \\ | на части.

    Args:
        product: экземпляр Product.

    Returns:
        Кортеж (категория, подкатегория), пустые строки если данных нет.
    """
    cat = getattr(product, 'catalog_category', None)
    if cat is not None:
        parent = getattr(cat, 'parent', None)
        if parent:
            return parent.name, cat.name
        return cat.name, ''

    raw = (getattr(product, 'category_1c', '') or '').strip()
    if not raw:
        return '', ''

    # "Тормозные системы / Диски тормозные" → ("Тормозные системы", "Диски тормозные")
    parts = [p.strip() for p in re.split(r'[/\\|]', raw) if p.strip()]
    return parts[0], parts[1] if len(parts) > 1 else ''


def _is_unreliable_article(article: str, brand: str) -> bool:
    """Определяет, является ли артикул ненадёжным для поиска.

    Возвращает True если артикул скорее всего НЕ является точным OEM/артикулом.
    Например: производитель = 'OEM' с коротким/общим номером.
    """
    if re.sub(r'[^a-zа-яё]+', '', str(brand or '').lower()) == 'oem':
        return True
    if article.upper().startswith('OEM'):
        return True
    return False


def _extract_keywords(text: str, max_words: int = 5) -> str:
    """Извлекает ключевые слова из описания товара.

    Убирает стоп-слова и ограничивает количество слов.
    """
    if not text:
        return ''
    # Стоп-слова для автозапчастей
    stop_words = {'для', 'и', 'в', 'на', 'с', 'по', 'из', 'к', 'от', 'до', 'не', 'а', 'но'}
    words = [w for w in text.split() if w.lower() not in stop_words and len(w) > 1]
    return ' '.join(words[:max_words])
