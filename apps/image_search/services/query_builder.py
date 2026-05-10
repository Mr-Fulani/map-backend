"""Формирование поисковых запросов для товара.

Использует реальные поля Product: brand, article, name.
Учитывает ненадёжные артикулы (OEM-префиксы, aftermarket).
"""

import re


def build_queries(product) -> list[tuple[str, str]]:
    """Формирует поисковые запросы с уровнем уверенности.

    Возвращает list[tuple[query, confidence_level]].
    confidence_level: 'HIGH', 'MEDIUM', 'LOW', 'VERY_LOW'.

    Args:
        product: экземпляр Product с полями brand, article, name.

    Returns:
        Список кортежей (запрос, уверенность).
    """
    queries = []
    pn = product.article.strip() if product.article else ''
    mfr = product.brand.strip() if product.brand else ''
    nom = product.name.strip() if product.name else ''

    # Q1: Артикул + производитель — самый точный
    if pn and not _is_unreliable_article(pn, mfr):
        queries.append((f'{pn} {mfr}', 'HIGH'))
        queries.append((f'{pn} автозапчасть', 'HIGH'))

    # Q2: Очищенный артикул (без спецсимволов)
    clean_pn = re.sub(r'[^A-Za-z0-9]', '', pn)
    if clean_pn and clean_pn != pn:
        queries.append((f'{clean_pn} {mfr}', 'MEDIUM'))

    # Q3: Описание (5 значимых слов) — когда артикул ненадёжен
    desc_words = _extract_keywords(nom, max_words=5)
    if desc_words:
        queries.append((f'{mfr} {desc_words}', 'LOW'))
        queries.append((desc_words, 'LOW'))

    return queries or [(nom, 'LOW')]


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
