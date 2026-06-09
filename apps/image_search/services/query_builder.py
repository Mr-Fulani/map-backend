"""Формирование поисковых запросов для товара.

Использует реальные поля Product: brand, article, name, category_1c, catalog_category.
Учитывает ненадёжные артикулы (OEM-префиксы, aftermarket).
"""

import re


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

    # Q1: Артикул + производитель — самый точный
    if pn and not _is_unreliable_article(pn, mfr):
        queries.append((f'{pn} {mfr}'.strip(), 'HIGH'))
        # Q1b: артикул + конкретная категория вместо generic "автозапчасть"
        hint = cat_hint or 'автозапчасть'
        queries.append((f'{pn} {hint}', 'HIGH'))

    # Q2: Очищенный артикул (без спецсимволов)
    clean_pn = re.sub(r'[^A-Za-z0-9]', '', pn)
    if clean_pn and clean_pn != pn:
        queries.append((f'{clean_pn} {mfr}'.strip(), 'MEDIUM'))

    # Q3: Производитель + ключевые слова названия + категория
    desc_words = _extract_keywords(nom, max_words=5)
    if desc_words:
        ctx = ' '.join(filter(None, [mfr, desc_words, cat_hint]))
        queries.append((ctx, 'LOW'))
        hint2 = cat_hint or ''
        queries.append((f'{desc_words} {hint2}'.strip() if hint2 else desc_words, 'LOW'))

    fallback = f'{nom} {cat_hint}'.strip() if cat_hint else nom
    return queries or [(fallback, 'LOW')]


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
