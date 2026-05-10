"""Тесты для query_builder: построение поисковых запросов по товару."""

from apps.image_search.services.query_builder import (
    _extract_keywords,
    _is_unreliable_article,
    build_queries,
)


class FakeProduct:
    """Заглушка Product для тестирования query_builder без БД."""

    def __init__(self, article='', brand='', name=''):
        self.article = article
        self.brand = brand
        self.name = name


class TestBuildQueries:
    """Тесты функции build_queries."""

    def test_артикул_и_бренд_дают_high_запрос(self):
        product = FakeProduct(article='25327H5010', brand='HYUNDAI-KIA')
        queries = build_queries(product)
        assert ('25327H5010 HYUNDAI-KIA', 'HIGH') in queries

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
