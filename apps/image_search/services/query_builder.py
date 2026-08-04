"""Формирование поисковых запросов для товара.

Использует реальные поля Product: brand, article, name, category_1c, catalog_category.
Учитывает ненадёжные артикулы (OEM-префиксы, aftermarket).
"""

import re


QUERY_BUILDER_VERSION = 'v3'


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
    mfr = product.brand.strip() if product.brand else ''
    nom = product.name.strip() if product.name else ''

    category, subcategory = _get_category_context(product)
    # Наиболее конкретный доступный уровень категории для уточнения запросов
    cat_hint = subcategory or category

    desc_words = _extract_keywords(nom, max_words=4)

    # Q1/Q2 deliberately contain identity and product context together. Search
    # providers currently execute only the first few queries, so weaker context
    # must not be deferred to the end of the list.
    if pn and not _is_unreliable_article(pn, mfr):
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

    for cross_code in _trusted_cross_codes(product)[:2]:
        queries.append((
            ' '.join(filter(None, [mfr, f'"{cross_code}"', cat_hint or desc_words])),
            'MEDIUM',
        ))

    fallback = f'{nom} {cat_hint}'.strip() if cat_hint else nom
    return _deduplicate_queries(queries) or [(fallback, 'LOW')]


def _trusted_cross_codes(product) -> list[str]:
    manager = getattr(product, 'cross_codes', None)
    if manager is None or not hasattr(manager, 'filter'):
        return []
    return list(
        manager.exclude(code='')
        .values_list('code', flat=True)[:2]
    )


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
    if brand.upper() == 'OEM' and len(article) < 6:
        return True
    if article.upper().startswith('OEM') and len(article) < 8:
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
