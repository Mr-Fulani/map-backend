from decimal import Decimal

import pytest

from apps.products.models import (
    GlobalPart, GlobalPartRelation, Product, ProductCrossCode,
)
from apps.products.part_parsers import ParsedCrossCode, ParsedPart
from apps.products.services import (
    ProductEnrichmentService, ProductKnowledgeGraphService,
)
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


@pytest.mark.django_db
def test_save_parsed_part_learns_global_cross_relations():
    tenant = make_tenant('kg-learn')
    product = make_product(tenant)
    parsed = ParsedPart(
        brand='BREMBO',
        article='P50136',
        title='Brembo P50136',
        source_url='https://tachka.ru/brembo/P50136',
        cross_codes=[
            ParsedCrossCode(
                manufacturer='MERCEDES-BENZ',
                code='A 000 420 60 00',
                code_type=ProductCrossCode.CodeType.OEM,
            ),
        ],
    )

    ProductEnrichmentService.save_parsed_part(tenant, product, parsed)

    source = GlobalPart.objects.get(normalized_brand='BREMBO', normalized_article='P50136')
    target = GlobalPart.objects.get(
        normalized_brand='MERCEDESBENZ',
        normalized_article='A0004206000',
    )
    relation = GlobalPartRelation.objects.get(source_part=source, target_part=target)
    assert relation.relation_type == GlobalPartRelation.RelationType.OEM
    assert relation.source_id == 'tachka'


@pytest.mark.django_db
def test_known_global_relations_apply_to_other_tenant_product():
    tenant_a = make_tenant('kg-owner')
    tenant_b = make_tenant('kg-consumer')
    product_a = make_product(tenant_a)
    product_b = make_product(tenant_b)
    parsed = ParsedPart(
        brand='BREMBO',
        article='P50136',
        cross_codes=[
            ParsedCrossCode(
                manufacturer='MERCEDES-BENZ',
                code='A0004206000',
                code_type=ProductCrossCode.CodeType.OEM,
            ),
        ],
    )
    ProductEnrichmentService.save_parsed_part(tenant_a, product_a, parsed)

    created = ProductKnowledgeGraphService.apply_known_relations_to_product(product_b)

    assert created == 1
    assert product_b.cross_codes.filter(
        tenant=tenant_b,
        manufacturer='MERCEDES-BENZ',
        normalized_code='A0004206000',
        code_type=ProductCrossCode.CodeType.OEM,
    ).exists()
    product_b.refresh_from_db()
    assert product_b.oem_numbers == ['A0004206000']


@pytest.mark.django_db
def test_unknown_global_relation_is_marked_for_review():
    tenant = make_tenant('kg-review')
    product = make_product(tenant)
    parsed = ParsedPart(
        brand='BREMBO',
        article='P50136',
        cross_codes=[
            ParsedCrossCode(
                manufacturer='UNKNOWN CATALOG',
                code='RAW-123',
                code_type=ProductCrossCode.CodeType.UNKNOWN,
            ),
        ],
    )

    ProductEnrichmentService.save_parsed_part(tenant, product, parsed)

    relation = GlobalPartRelation.objects.get(
        source_part__normalized_brand='BREMBO',
        target_part__normalized_article='RAW123',
    )
    assert relation.relation_type == GlobalPartRelation.RelationType.UNKNOWN
    assert relation.needs_review is True
    assert relation.confidence == 0.8


@pytest.mark.django_db
def test_reviewable_global_relations_are_not_applied_to_tenant_product():
    tenant = make_tenant('kg-skip-review')
    product = make_product(tenant)
    source = ProductKnowledgeGraphService.upsert_part(
        brand='BREMBO',
        article='P50136',
        source_id='tachka',
    )
    target = ProductKnowledgeGraphService.upsert_part(
        brand='UNKNOWN CATALOG',
        article='RAW-123',
        source_id='tachka',
        needs_review=True,
    )
    ProductKnowledgeGraphService.upsert_relation(
        source,
        target,
        GlobalPartRelation.RelationType.UNKNOWN,
        source_id='tachka',
        needs_review=True,
    )

    created = ProductKnowledgeGraphService.apply_known_relations_to_product(product)

    assert created == 0
    assert product.cross_codes.count() == 0
