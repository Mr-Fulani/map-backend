"""Тесты для query_builder: построение поисковых запросов по товару."""

from apps.image_search.services.query_builder import (
    _extract_keywords,
    _get_category_context,
    _is_unreliable_article,
    build_queries,
)


class FakeCategory:
    """Заглушка TenantCatalogCategory для тестирования."""

    def __init__(self, name, parent=None):
        self.name = name
        self.parent = parent


class FakeProduct:
    """Заглушка Product для тестирования query_builder без БД."""

    def __init__(self, article='', brand='', name='', category_1c='', catalog_category=None):
        self.article = article
        self.brand = brand
        self.name = name
        self.category_1c = category_1c
        self.catalog_category = catalog_category


class TestBuildQueries:
    """Тесты функции build_queries."""

    def test_артикул_и_бренд_дают_high_запрос(self):
        product = FakeProduct(article='25327H5010', brand='HYUNDAI-KIA')
        queries = build_queries(product)
        assert queries[0] == ('HYUNDAI-KIA "25327H5010" автозапчасть', 'HIGH')

    def test_первые_запросы_содержат_идентичность_и_контекст(self):
        product = FakeProduct(
            article='P50136', brand='BREMBO', name='Колодки тормозные задние',
            category_1c='Тормозная система / Колодки',
        )

        queries = build_queries(product)

        assert all('BREMBO' in query and 'P50136' in query for query, _ in queries[:2])
        assert any('Колодки' in query for query, _ in queries[:2])

    def test_пустые_поля_возвращают_хотя_бы_один_запрос(self):
        product = FakeProduct(article='', brand='', name='Прокладка ГБЦ')
        queries = build_queries(product)
        assert len(queries) >= 1

    def test_ненадёжный_oem_артикул_не_даёт_high(self):
        product = FakeProduct(article='12345', brand='OEM')
        queries = build_queries(product)
        confidences = [conf for _, conf in queries]
        assert 'HIGH' not in confidences

    def test_очищенный_артикул_дает_medium_запрос(self):
        # Артикул со спецсимволами → добавляется очищенный вариант
        product = FakeProduct(article='253-27.H50', brand='BOSCH')
        queries = build_queries(product)
        clean_queries = [(q, c) for q, c in queries if c == 'MEDIUM']
        assert len(clean_queries) > 0

    def test_совпадающий_очищенный_артикул_не_дублируется(self):
        # Если очищенный == исходный — не добавлять MEDIUM дубль
        product = FakeProduct(article='ABC123', brand='NGK')
        queries = build_queries(product)
        medium_queries = [q for q, c in queries if c == 'MEDIUM']
        assert len(medium_queries) == 0

    def test_категория_из_category_1c_добавляется_в_q1b(self):
        product = FakeProduct(
            article='A123', brand='BOSCH',
            category_1c='Тормозные системы / Диски тормозные',
        )
        queries = build_queries(product)
        high_queries = [q for q, c in queries if c == 'HIGH']
        assert any('Диски тормозные' in q for q in high_queries)

    def test_категория_из_catalog_category_добавляется_в_q1b(self):
        subcat = FakeCategory('Масляные фильтры', parent=FakeCategory('Фильтры'))
        product = FakeProduct(article='B456', brand='MANN', catalog_category=subcat)
        queries = build_queries(product)
        high_queries = [q for q, c in queries if c == 'HIGH']
        assert any('Масляные фильтры' in q for q in high_queries)

    def test_без_категории_q1b_использует_автозапчасть(self):
        product = FakeProduct(article='C789', brand='NGK')
        queries = build_queries(product)
        high_queries = [q for q, c in queries if c == 'HIGH']
        assert any('автозапчасть' in q for q in high_queries)

    def test_категория_добавляется_в_low_запросы(self):
        product = FakeProduct(
            article='', brand='BOSCH', name='Фильтр масляный',
            category_1c='Фильтры',
        )
        queries = build_queries(product)
        low_queries = [q for q, c in queries if c == 'LOW']
        assert any('Фильтры' in q for q in low_queries)


class TestGetCategoryContext:
    """Тесты функции _get_category_context."""

    def test_catalog_category_без_родителя(self):
        product = FakeProduct(catalog_category=FakeCategory('Фильтры'))
        category, subcategory = _get_category_context(product)
        assert category == 'Фильтры'
        assert subcategory == ''

    def test_catalog_category_с_родителем(self):
        parent = FakeCategory('Тормозная система')
        child = FakeCategory('Тормозные диски', parent=parent)
        product = FakeProduct(catalog_category=child)
        category, subcategory = _get_category_context(product)
        assert category == 'Тормозная система'
        assert subcategory == 'Тормозные диски'

    def test_category_1c_с_разделителем_слэш(self):
        product = FakeProduct(category_1c='Тормозные системы / Диски тормозные')
        category, subcategory = _get_category_context(product)
        assert category == 'Тормозные системы'
        assert subcategory == 'Диски тормозные'

    def test_category_1c_без_разделителя(self):
        product = FakeProduct(category_1c='Фильтры')
        category, subcategory = _get_category_context(product)
        assert category == 'Фильтры'
        assert subcategory == ''

    def test_без_категории_возвращает_пустые_строки(self):
        product = FakeProduct()
        category, subcategory = _get_category_context(product)
        assert category == ''
        assert subcategory == ''

    def test_catalog_category_имеет_приоритет_над_category_1c(self):
        product = FakeProduct(
            catalog_category=FakeCategory('Свечи зажигания'),
            category_1c='Другая категория',
        )
        category, _ = _get_category_context(product)
        assert category == 'Свечи зажигания'


class TestIsUnreliableArticle:
    """Тесты вспомогательной функции _is_unreliable_article."""

    def test_короткий_oem_ненадёжен(self):
        assert _is_unreliable_article('1234', 'OEM') is True

    def test_длинный_oem_надёжен(self):
        assert _is_unreliable_article('123456789', 'OEM') is False

    def test_артикул_с_oem_префиксом_ненадёжен(self):
        assert _is_unreliable_article('OEM1234', 'BOSCH') is True

    def test_нормальный_артикул_надёжен(self):
        assert _is_unreliable_article('25327H5010', 'HYUNDAI-KIA') is False


class TestExtractKeywords:
    """Тесты вспомогательной функции _extract_keywords."""

    def test_удаляет_стоп_слова(self):
        result = _extract_keywords('Прокладка для двигателя и коробки')
        words = result.split()
        assert 'для' not in words
        assert 'и' not in words

    def test_ограничивает_количество_слов(self):
        result = _extract_keywords('один два три четыре пять шесть семь', max_words=3)
        assert len(result.split()) == 3

    def test_пустая_строка_возвращает_пустую(self):
        assert _extract_keywords('') == ''
