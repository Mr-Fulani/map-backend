from apps.image_search.services.candidate_filter import candidate_metadata_assessment
from apps.image_search.sources.base import ImageCandidate


class ProductStub:
    article = 'P 50 136'
    brand = 'BREMBO'
    name = 'Колодки тормозные задние'
    category_1c = 'Тормозная система / Колодки'
    catalog_category = None


def candidate(url, title='', tier=3):
    return ImageCandidate(
        url=url,
        source_id='brave',
        tier=tier,
        raw_meta={'title': title, 'confidence': 'HIGH'},
    )


def test_exact_article_in_url_is_allowed():
    allowed, reasons, relevance = candidate_metadata_assessment(
        ProductStub(),
        candidate('https://img.example.com/brembo-P50136-product.jpg'),
    )
    assert allowed is True
    assert 'article_match' in reasons
    assert relevance >= 0.55


def test_unrelated_web_result_is_rejected():
    allowed, reasons, _ = candidate_metadata_assessment(
        ProductStub(),
        candidate('https://example.com/travel.jpg', 'Красивый автомобиль у моря'),
    )
    assert allowed is False
    assert reasons == ['insufficient_product_identity']


def test_tracking_and_logo_images_are_rejected():
    assert candidate_metadata_assessment(
        ProductStub(), candidate('https://in.getclicky.com/123.gif'),
    )[0] is False
    assert candidate_metadata_assessment(
        ProductStub(), candidate('https://img.example.com/brandlogos/brembo.png'),
    )[0] is False


def test_exact_catalog_source_can_omit_article_from_cdn_url():
    allowed, reasons, _ = candidate_metadata_assessment(
        ProductStub(),
        candidate('https://catalog.example.com/assets/abc.jpg', tier=1),
    )
    assert allowed is True
    assert reasons == ['catalog_source']


def test_short_article_does_not_match_long_unrelated_numeric_id():
    product = ProductStub()
    product.article = '123'

    allowed, reasons, _ = candidate_metadata_assessment(
        product,
        candidate(
            'https://example.com/catalog/123456/photo.jpg',
            'Деталь другого производителя',
        ),
    )

    assert allowed is False
    assert reasons == ['insufficient_product_identity']
