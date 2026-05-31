from decimal import Decimal

import pytest

from apps.products.enrichment import make_value_hash, normalize_part_code
from apps.products.models import (
    Product, ProductCatalogClassification, ProductCrossCode, ProductEnrichmentFact,
    TenantCatalogCategory, TenantCategoryMapping,
)
from apps.products.part_parsers import ParsedFitment, ParsedPart
from apps.products.services import ProductEnrichmentService
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_product(tenant, article='P50136', brand='BREMBO'):
    return Product.objects.create(
        tenant=tenant,
        article=article,
        brand=brand,
        name=f'{brand} {article}',
        price=Decimal('0'),
        stock_qty=0,
    )


def test_normalize_part_code_keeps_leading_zeroes():
    assert normalize_part_code('P 50 136') == 'P50136'
    assert normalize_part_code('P-50-136') == 'P50136'
    assert normalize_part_code('p 50 136') == 'P50136'
    assert normalize_part_code('A 000 420 60 00') == 'A0004206000'
    assert normalize_part_code('0004206000') == '0004206000'


@pytest.mark.django_db
def test_enrichment_records_are_tenant_scoped_for_same_article():
    tenant_a = make_tenant('parts-a')
    tenant_b = make_tenant('parts-b')
    product_a = make_product(tenant_a)
    product_b = make_product(tenant_b)

    ProductEnrichmentService.create_cross_code(
        tenant=tenant_a,
        product=product_a,
        manufacturer='MERCEDES-BENZ',
        code='A 000 420 60 00',
        normalized_code=normalize_part_code('A 000 420 60 00'),
        code_type=ProductCrossCode.CodeType.OEM,
    )
    ProductEnrichmentService.create_cross_code(
        tenant=tenant_b,
        product=product_b,
        manufacturer='MERCEDES-BENZ',
        code='A 000 420 60 00',
        normalized_code=normalize_part_code('A 000 420 60 00'),
        code_type=ProductCrossCode.CodeType.OEM,
    )

    assert ProductCrossCode.objects.filter(tenant=tenant_a).count() == 1
    assert ProductCrossCode.objects.filter(tenant=tenant_b).count() == 1


@pytest.mark.django_db
def test_enrichment_models_reject_cross_tenant_product():
    tenant_a = make_tenant('owner-a')
    tenant_b = make_tenant('owner-b')
    product_b = make_product(tenant_b)

    with pytest.raises(ValueError):
        ProductEnrichmentService.create_attribute(
            tenant=tenant_a,
            product=product_b,
            name='Ширина',
            value='114 мм',
        )


@pytest.mark.django_db
def test_parse_job_rejects_cross_tenant_product():
    tenant_a = make_tenant('job-a')
    tenant_b = make_tenant('job-b')
    product_b = make_product(tenant_b)

    with pytest.raises(ValueError):
        ProductEnrichmentService.create_parse_job(
            tenant=tenant_a,
            product=product_b,
            brand='BREMBO',
            article='P50136',
            normalized_article='P50136',
        )


@pytest.mark.django_db
def test_catalog_classification_detects_auto_part():
    tenant = make_tenant('classify-auto')
    product = make_product(tenant, article='P50136', brand='BREMBO')
    product.name = 'Колодки тормозные BREMBO P50136'
    product.save(update_fields=['name'])

    classification = ProductEnrichmentService.classify_product_catalog_domain(product)

    assert classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
    assert classification.confidence >= 0.7
    assert classification.reason


@pytest.mark.django_db
def test_catalog_classification_detects_jewellery():
    tenant = make_tenant('classify-jewellery')
    product = make_product(tenant, article='RING1', brand='NO_BRAND')
    product.name = 'Золотое кольцо'
    product.category_1c = 'Украшения'
    product.save(update_fields=['name', 'category_1c'])

    classification = ProductEnrichmentService.classify_product_catalog_domain(product)

    assert classification.domain == ProductCatalogClassification.Domain.JEWELLERY
    assert classification.needs_review is False


@pytest.mark.django_db
def test_catalog_classification_uses_tenant_category_mapping():
    tenant = make_tenant('classify-category-map')
    product = make_product(tenant, article='X1', brand='NO_BRAND')
    product.name = 'Неочевидное название'
    product.category_1c = 'Тормоза'
    product.save(update_fields=['name', 'category_1c'])
    category = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Тормозные колодки',
        domain=TenantCatalogCategory.Domain.AUTO_PARTS,
    )
    TenantCategoryMapping.objects.create(
        tenant=tenant,
        source_category='Тормоза',
        category=category,
    )

    classification = ProductEnrichmentService.classify_product_catalog_domain(product)

    product.refresh_from_db()
    assert product.catalog_category == category
    assert classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
    assert 'категории каталога' in classification.reason


@pytest.mark.django_db
def test_catalog_classification_does_not_overwrite_manual_source_without_force():
    tenant = make_tenant('classify-manual-keep')
    product = make_product(tenant, article='P50136', brand='BREMBO')
    product.name = 'Колодки тормозные BREMBO P50136'
    product.save(update_fields=['name'])
    manual = ProductCatalogClassification.objects.create(
        tenant=tenant,
        product=product,
        domain=ProductCatalogClassification.Domain.GENERIC,
        confidence=1,
        source=ProductCatalogClassification.Source.MANUAL,
        reason='Оператор проверил товар вручную.',
        needs_review=False,
    )

    classification = ProductEnrichmentService.classify_product_catalog_domain(product)

    manual.refresh_from_db()
    assert classification.pk == manual.pk
    assert manual.domain == ProductCatalogClassification.Domain.GENERIC
    assert manual.source == ProductCatalogClassification.Source.MANUAL
    assert manual.reason == 'Оператор проверил товар вручную.'


@pytest.mark.django_db
def test_catalog_classification_force_can_overwrite_manual_source():
    tenant = make_tenant('classify-manual-force')
    product = make_product(tenant, article='P50136', brand='BREMBO')
    product.name = 'Колодки тормозные BREMBO P50136'
    product.save(update_fields=['name'])
    ProductCatalogClassification.objects.create(
        tenant=tenant,
        product=product,
        domain=ProductCatalogClassification.Domain.GENERIC,
        confidence=1,
        source=ProductCatalogClassification.Source.MANUAL,
        reason='Оператор проверил товар вручную.',
        needs_review=False,
    )

    classification = ProductEnrichmentService.classify_product_catalog_domain(product, force=True)

    assert classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
    assert classification.source == ProductCatalogClassification.Source.RULES
    assert classification.reason.startswith('Найдены признаки автозапчасти')


@pytest.mark.django_db
def test_value_hash_is_filled_for_text_based_unique_constraints():
    tenant = make_tenant('hash-co')
    product = make_product(tenant)

    attr = ProductEnrichmentService.create_attribute(
        tenant=tenant,
        product=product,
        name='Толщина',
        value='16 мм',
    )
    fact = ProductEnrichmentService.create_fact(
        tenant=tenant,
        product=product,
        fact_type=ProductEnrichmentFact.FactType.TECHNICAL,
        name='Толщина',
        value='16 мм',
    )
    ProductEnrichmentService.create_fitment(
        tenant=tenant,
        product=product,
        make='MERCEDES-BENZ',
        model='E-CLASS',
        generation='W213',
    )

    assert attr.value_hash == make_value_hash('16 мм')
    assert fact.value_hash == make_value_hash('16 мм')
    assert product.fitments.filter(model='E-CLASS').exists()


@pytest.mark.django_db
def test_conflicting_source_fitment_is_kept_for_review_without_polluting_applicability():
    tenant = make_tenant('fitment-conflict')
    product = make_product(tenant)
    trusted = ParsedPart(
        brand='BREMBO',
        article='P50136',
        source_url='https://tachka.ru/brembo/P50136',
        fitments=[
            ParsedFitment(
                make='MERCEDES-BENZ',
                model='E-CLASS',
                generation='W213',
                date_from='01.2016',
                date_to='2023',
                modification='E 220 d',
                engine_code='213.005',
                power_hp=194,
                confidence=0.95,
            ),
        ],
    )
    conflicting = ParsedPart(
        brand='BREMBO',
        article='P50136',
        source_url='https://example.test/part/P50136',
        fitments=[
            ParsedFitment(
                make='MERCEDES-BENZ',
                model='E-CLASS',
                generation='W213',
                date_from='2017',
                date_to='2023',
                modification='E 220 d',
                engine_code='213.005',
                power_hp=194,
                confidence=1.0,
            ),
        ],
    )

    ProductEnrichmentService.save_parsed_part(tenant, product, trusted, source_id='tachka')
    ProductEnrichmentService.save_parsed_part(
        tenant, product, conflicting, source_id='unregistered-source',
    )

    reviewable = product.fitments.get(source_id='unregistered-source')
    product.refresh_from_db()
    assert reviewable.needs_review is True
    assert reviewable.source_url == 'https://example.test/part/P50136'
    assert product.fitments.count() == 2
    assert product.applicability == [{
        'make': 'MERCEDES-BENZ',
        'model': 'E-CLASS',
        'generation': 'W213',
        'date_from': '01.2016',
        'date_to': '2023',
        'modification': 'E 220 d',
        'engine_code': '213.005',
        'power_hp': 194,
        'source_id': 'tachka',
    }]


@pytest.mark.django_db
def test_conflicting_description_fact_keeps_provenance_and_needs_review():
    tenant = make_tenant('fact-conflict')
    product = make_product(tenant)
    first = ParsedPart(
        brand='BREMBO',
        article='P50136',
        source_url='https://tachka.ru/brembo/P50136',
        description_facts={'material': 'ceramic'},
    )
    second = ParsedPart(
        brand='BREMBO',
        article='P50136',
        source_url='https://example.test/part/P50136',
        description_facts={'material': 'metallic'},
    )

    ProductEnrichmentService.save_parsed_part(tenant, product, first, source_id='tachka')
    ProductEnrichmentService.save_parsed_part(product.tenant, product, second, source_id='second-source')

    fact = ProductEnrichmentFact.objects.get(source_id='second-source')
    assert fact.needs_review is True
    assert fact.source_url == 'https://example.test/part/P50136'
    assert fact.last_seen_at is not None
