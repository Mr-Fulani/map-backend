from decimal import Decimal

import pytest

from apps.products.models import (
    GlobalPart, GlobalPartFitment, GlobalPartRelation, PartCategory, Product,
    ProductBrand, ProductCrossCode,
    VehicleMake, VehicleModel,
)
from apps.products.part_parsers import (
    ParsedCrossCode, ParsedFitment, ParsedPart, ParsedRelatedPart,
)
from apps.products.services import (
    ProductCategorySeedService, ProductEnrichmentService, ProductKnowledgeGraphService,
    VehicleKnowledgeService,
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
def test_save_parsed_part_learns_global_related_parts():
    tenant = make_tenant('kg-related')
    product = make_product(tenant, article='485108Z460', brand='TOYOTA-LEXUS')
    parsed = ParsedPart(
        brand='TOYOTA-LEXUS',
        article='485108Z460',
        source_url='https://tachka.ru/poisk?search=485108Z460',
        related_parts=[
            ParsedRelatedPart(
                brand='KYB',
                article='3350048',
                title='Амортизатор TOYOTA CAMRY 17- газ.пер.прав. KYB',
                relation_type=GlobalPartRelation.RelationType.ANALOGUE,
                raw_text='Амортизатор TOYOTA CAMRY 17- газ.пер.прав. KYB. Артикул 3350048',
                confidence=0.9,
            ),
        ],
    )

    ProductEnrichmentService.save_parsed_part(tenant, product, parsed)

    relation = GlobalPartRelation.objects.get(
        source_part__normalized_article='485108Z460',
        target_part__normalized_brand='KYB',
        target_part__normalized_article='3350048',
    )
    assert relation.relation_type == GlobalPartRelation.RelationType.ANALOGUE
    assert relation.needs_review is False


@pytest.mark.django_db
def test_save_parsed_part_learns_global_fitments():
    tenant = make_tenant('kg-fitment-learn')
    product = make_product(tenant)
    parsed = ParsedPart(
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
                modification='E 220 d 4-matic',
                engine_code='213.005',
                power_hp=194,
                raw_text='E-CLASS (W213) 01.2016-2023 E 220 d 4-matic (213.005) 194 л.с',
                confidence=0.95,
            ),
        ],
    )

    ProductEnrichmentService.save_parsed_part(tenant, product, parsed)

    fitment = GlobalPartFitment.objects.get(
        part__normalized_brand='BREMBO',
        part__normalized_article='P50136',
        make='MERCEDES-BENZ',
        model='E-CLASS',
    )
    assert fitment.generation == 'W213'
    assert fitment.power_hp == 194
    assert fitment.needs_review is False
    assert fitment.vehicle_make.normalized_name == 'MERCEDESBENZ'
    assert fitment.vehicle_model.normalized_name == 'ECLASS'


@pytest.mark.django_db
def test_global_part_keeps_brand_article_identity_with_brand_reference():
    tenant = make_tenant('kg-brand-ref')
    product = make_product(tenant, brand='BREMBO', article='P50136')
    parsed = ParsedPart(brand='Brembo', article='P50136')

    ProductEnrichmentService.save_parsed_part(tenant, product, parsed)

    part = GlobalPart.objects.get(normalized_brand='BREMBO', normalized_article='P50136')
    assert part.brand == 'BREMBO'
    assert part.brand_ref.name == 'Brembo'
    assert ProductBrand.objects.filter(normalized_name='BREMBO').count() == 1


@pytest.mark.django_db
def test_vehicle_knowledge_service_merges_make_aliases():
    first = VehicleKnowledgeService.upsert_make('MERCEDES-BENZ')
    second = VehicleKnowledgeService.upsert_make('MB')
    third = VehicleKnowledgeService.upsert_make('Mercedes')

    assert first == second == third
    assert VehicleMake.objects.count() == 1
    assert first.normalized_name == 'MERCEDESBENZ'
    first.refresh_from_db()
    assert 'MB' in first.aliases
    assert 'Mercedes' in first.aliases


@pytest.mark.django_db
def test_vehicle_knowledge_service_scopes_models_by_make():
    mercedes = VehicleKnowledgeService.upsert_make('Mercedes-Benz')
    toyota = VehicleKnowledgeService.upsert_make('Toyota')

    mercedes_model = VehicleKnowledgeService.upsert_model(mercedes, 'E-CLASS')
    same_mercedes_model = VehicleKnowledgeService.upsert_model(mercedes, 'E CLASS')
    toyota_model = VehicleKnowledgeService.upsert_model(toyota, 'E-CLASS')

    assert mercedes_model == same_mercedes_model
    assert toyota_model != mercedes_model
    assert VehicleModel.objects.count() == 2


@pytest.mark.django_db
def test_part_category_keeps_fitment_requirement_flag():
    category = PartCategory.objects.create(
        name='Тормозные колодки',
        normalized_name='BRAKEPADS',
        aliases=['Колодки тормозные'],
        fitment_required=True,
    )

    assert str(category) == 'Тормозные колодки'
    assert category.fitment_required is True


@pytest.mark.django_db
def test_base_part_categories_seed_platform_and_tenant_catalogs():
    tenant = make_tenant('part-category-seed')
    initial_tenant_count = tenant.catalog_categories.count()

    created_count = ProductCategorySeedService.seed_platform_categories()
    ProductCategorySeedService.seed_tenant_default_categories(tenant)

    assert created_count == 0
    assert PartCategory.objects.filter(name='Тормозная система', parent__isnull=True).exists()
    assert PartCategory.objects.filter(name='Тормозные колодки', parent__name='Тормозная система').exists()
    assert PartCategory.objects.get(name='Моторные масла').fitment_required is False
    assert tenant.catalog_categories.filter(name='Тормозная система', parent__isnull=True).exists()
    assert tenant.catalog_categories.filter(name='Тормозные колодки', parent__name='Тормозная система').exists()
    assert tenant.catalog_categories.count() == initial_tenant_count


@pytest.mark.django_db
def test_known_global_fitments_apply_to_other_tenant_product():
    tenant_a = make_tenant('kg-fitment-owner')
    tenant_b = make_tenant('kg-fitment-consumer')
    product_a = make_product(tenant_a)
    product_b = make_product(tenant_b)
    parsed = ParsedPart(
        brand='BREMBO',
        article='P50136',
        fitments=[
            ParsedFitment(
                make='MERCEDES-BENZ',
                model='E-CLASS',
                generation='W213',
                modification='E 220 d',
                engine_code='213.005',
                power_hp=194,
                confidence=0.95,
            ),
        ],
    )
    ProductEnrichmentService.save_parsed_part(tenant_a, product_a, parsed)

    created = ProductKnowledgeGraphService.apply_known_fitments_to_product(product_b)

    assert created == 1
    assert product_b.fitments.filter(
        tenant=tenant_b,
        make='MERCEDES-BENZ',
        model='E-CLASS',
        generation='W213',
    ).exists()
    product_b.refresh_from_db()
    assert product_b.applicability[0]['make'] == 'MERCEDES-BENZ'
    assert product_b.applicability[0]['model'] == 'E-CLASS'


@pytest.mark.django_db
def test_reviewable_global_fitments_are_not_applied_to_tenant_product():
    tenant = make_tenant('kg-fitment-review')
    product = make_product(tenant)
    part = ProductKnowledgeGraphService.upsert_part(
        brand='BREMBO',
        article='P50136',
        source_id='tachka',
    )
    ProductKnowledgeGraphService.upsert_fitment(
        part=part,
        fitment=ParsedFitment(
            make='',
            model='E-CLASS',
            generation='W213',
            needs_review=True,
        ),
        source_id='tachka',
    )

    created = ProductKnowledgeGraphService.apply_known_fitments_to_product(product)

    assert created == 0
    assert product.fitments.count() == 0


@pytest.mark.django_db
def test_low_confidence_global_fitments_are_not_applied_to_tenant_product():
    tenant = make_tenant('kg-fitment-low-confidence')
    product = make_product(tenant)
    part = ProductKnowledgeGraphService.upsert_part(
        brand='BREMBO',
        article='P50136',
        source_id='tachka',
    )
    ProductKnowledgeGraphService.upsert_fitment(
        part=part,
        fitment=ParsedFitment(
            make='MERCEDES-BENZ',
            model='E-CLASS',
            generation='W213',
            confidence=0.7,
        ),
        source_id='tachka',
    )

    created = ProductKnowledgeGraphService.apply_known_fitments_to_product(product)

    assert created == 0
    assert product.fitments.count() == 0


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


@pytest.mark.django_db
def test_low_confidence_global_relations_are_not_applied_to_tenant_product():
    tenant = make_tenant('kg-relation-low-confidence')
    product = make_product(tenant)
    source = ProductKnowledgeGraphService.upsert_part(
        brand='BREMBO',
        article='P50136',
        source_id='tachka',
    )
    target = ProductKnowledgeGraphService.upsert_part(
        brand='MERCEDES-BENZ',
        article='A0004206000',
        source_id='tachka',
    )
    ProductKnowledgeGraphService.upsert_relation(
        source,
        target,
        GlobalPartRelation.RelationType.OEM,
        source_id='tachka',
        confidence=0.7,
    )

    created = ProductKnowledgeGraphService.apply_known_relations_to_product(product)

    assert created == 0
    assert product.cross_codes.count() == 0


@pytest.mark.django_db
def test_global_part_confidence_is_not_raised_by_untrusted_source():
    part = ProductKnowledgeGraphService.upsert_part(
        brand='BREMBO',
        article='P50136',
        source_id='tachka',
        confidence=0.8,
    )

    ProductKnowledgeGraphService.upsert_part(
        brand='BREMBO',
        article='P50136',
        source_id='unregistered-source',
        confidence=1.0,
    )

    part.refresh_from_db()
    assert part.source_id == 'tachka'
    assert part.confidence == 0.8
