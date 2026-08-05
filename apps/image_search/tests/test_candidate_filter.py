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


class FitmentManager:
    def __init__(self, fitments):
        self.fitments = fitments

    def all(self):
        return self.fitments


class Fitment:
    make = 'Kia'
    model = 'Optima 4'
    generation = 'JF'
    source_id = 'manual'
    confidence = 1.0
    needs_review = False
    review_status = 'approved'


class InternalArticleProduct:
    article = 'OEM0099FONR'
    brand = ''
    name = 'Фонарь правый внешний Kia Optima 4 JF (2016-2020)'
    category_1c = ''
    catalog_category = None

    def __init__(self):
        self.fitments = FitmentManager([Fitment()])


class InternalArticleProductWithoutFitment(InternalArticleProduct):
    def __init__(self):
        self.fitments = FitmentManager([])


def test_internal_article_allows_strong_vehicle_and_part_context():
    allowed, reasons, relevance = candidate_metadata_assessment(
        InternalArticleProduct(),
        candidate(
            'https://example.com/kia-optima-jf-right-tail-light.jpg',
            'Фонарь задний внешний правый Kia Optima JF 2016-2020',
        ),
    )

    assert allowed is True
    assert 'vehicle_and_part_context_match' in reasons
    assert relevance >= 0.35


def test_internal_article_rejects_other_vehicle_model():
    allowed, reasons, _ = candidate_metadata_assessment(
        InternalArticleProduct(),
        candidate(
            'https://example.com/kia-ceed-tail-light.jpg',
            'Фонарь внешний KIA Ceed 2012-2018 JD',
        ),
    )

    assert allowed is False
    assert reasons == ['insufficient_product_identity']


def test_name_identity_works_without_saved_fitment():
    allowed, reasons, _ = candidate_metadata_assessment(
        InternalArticleProductWithoutFitment(),
        candidate(
            'https://example.com/kia-optima-tail-light.jpg',
            'Фонарь правый Kia Optima JF',
        ),
    )

    assert allowed is True
    assert 'vehicle_and_part_context_match' in reasons


def test_name_identity_without_fitment_still_rejects_ceed():
    allowed, reasons, _ = candidate_metadata_assessment(
        InternalArticleProductWithoutFitment(),
        candidate(
            'https://example.com/kia-ceed-tail-light.jpg',
            'Фонарь внешний KIA Ceed 2012-2018 JD',
        ),
    )

    assert allowed is False
    assert reasons == ['insufficient_product_identity']


def test_context_match_rejects_conflicting_side():
    allowed, reasons, _ = candidate_metadata_assessment(
        InternalArticleProduct(),
        candidate(
            'https://example.com/kia-optima-jf-left-tail-light.jpg',
            'Фонарь задний внешний левый Kia Optima JF 2016-2020',
        ),
    )

    assert allowed is False
    assert reasons == ['conflicting_side_or_position']
