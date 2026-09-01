from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client
from django.utils import timezone

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import (
    Listing,
    MarketplaceAccount,
    MarketplaceFeedRun,
    OzonAccountProfile,
    OzonAttributeValueSnapshot,
    OzonCategoryAttributeSnapshot,
    OzonCategoryPolicy,
    OzonCategoryTreeSnapshot,
    OzonOfferDraft,
)
from apps.marketplaces.ozon_catalog import (
    normalize_category_attributes,
    normalize_category_tree,
)
from apps.marketplaces.ozon_autofill import schedule_ozon_autofill
from apps.products.models import Product, ProductImage, ProductPhysicalProfile
from apps.tenants.tests.auth import create_tenant_with_operator_key, owner_access_token


VALUE_SEARCH = (
    'apps.marketplaces.ozon_catalog.OzonSellerClient.'
    'search_description_category_attribute_values'
)


def _tenant(slug):
    return create_tenant_with_operator_key(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )


def _account(tenant, external_id, *, marketplace='ozon'):
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=marketplace,
        name=f'{marketplace} {external_id}',
        external_id=external_id,
        credentials_enc=encrypt({
            'client_id': external_id,
            'api_key' if marketplace == 'ozon' else 'client_secret': 'secret',
        }),
    )
    if marketplace == 'ozon':
        OzonAccountProfile.objects.create(
            account=account,
            connection_status=OzonAccountProfile.ConnectionStatus.CONNECTED,
            selected_warehouse_id='warehouse-1',
            selected_warehouse_name='Склад 1',
            last_checked_at=timezone.now(),
        )
    return account


def _product(tenant, article='OZ-1'):
    return Product.objects.create(
        tenant=tenant,
        article=article,
        name='Амортизатор',
        brand='Test Brand',
        price=Decimal('1000'),
        stock_qty=2,
        description_ai='Описание товара',
    )


def _catalog(account):
    tree, node_count, active_type_count = normalize_category_tree([{
        'description_category_id': 101,
        'category_name': 'Автотовары',
        'disabled': False,
        'children': [{
            'type_id': 202,
            'type_name': 'Амортизатор',
            'disabled': False,
            'children': [],
        }],
    }])
    return OzonCategoryTreeSnapshot.objects.create(
        account=account,
        language='DEFAULT',
        schema_hash='a' * 64,
        tree=tree,
        node_count=node_count,
        active_type_count=active_type_count,
    )


def _attribute_catalog(account, *, revision='b' * 64):
    attributes = normalize_category_attributes([{
        'id': 85,
        'attribute_complex_id': 0,
        'name': 'Бренд',
        'description': 'Выберите бренд',
        'type': 'String',
        'is_collection': False,
        'is_required': True,
        'is_aspect': False,
        'max_value_count': 1,
        'group_name': 'Основные',
        'group_id': 1,
        'dictionary_id': 42,
        'category_dependent': True,
        'complex_is_collection': False,
    }])
    return OzonCategoryAttributeSnapshot.objects.create(
        account=account,
        description_category_id=101,
        type_id=202,
        language='DEFAULT',
        schema_hash=revision,
        attributes=attributes,
        attribute_count=1,
        required_attribute_count=1,
    )


def _value_snapshot(account, schema, *, value='Canonical Brand'):
    return OzonAttributeValueSnapshot.objects.create(
        account=account,
        description_category_id=101,
        type_id=202,
        attribute_id=85,
        language='DEFAULT',
        query='Brand',
        attribute_schema_hash=schema.schema_hash,
        schema_hash='c' * 64,
        values=[{'id': 501, 'value': value, 'info': '', 'picture': ''}],
        value_count=1,
    )


def _autofill_attribute_catalog(account):
    raw_attributes = [
        (1, 'Название модели', 0),
        (2, 'Код ТН ВЭД', 0),
        (3, 'Нужен код маркировки', 71),
        (4, 'Бренд', 72),
        (5, 'Партномер (артикул производителя)', 0),
        (6, 'Тип', 73),
    ]
    attributes = normalize_category_attributes([{
        'id': attribute_id,
        'attribute_complex_id': 0,
        'name': name,
        'description': '',
        'type': 'String',
        'is_collection': False,
        'is_required': True,
        'is_aspect': False,
        'max_value_count': 1,
        'group_name': 'Основные',
        'group_id': 1,
        'dictionary_id': dictionary_id,
        'category_dependent': True,
        'complex_is_collection': False,
    } for attribute_id, name, dictionary_id in raw_attributes])
    return OzonCategoryAttributeSnapshot.objects.create(
        account=account,
        description_category_id=101,
        type_id=202,
        language='DEFAULT',
        schema_hash='e' * 64,
        attributes=attributes,
        attribute_count=len(attributes),
        required_attribute_count=len(attributes),
    )


def _strict_input_attribute_catalog(account):
    attributes = normalize_category_attributes([{
        'id': 2,
        'attribute_complex_id': 0,
        'name': 'ТН ВЭД коды ЕАЭС',
        'description': 'Выберите значение из справочника',
        'type': 'String',
        'is_collection': False,
        'is_required': True,
        'is_aspect': False,
        'max_value_count': 1,
        'group_name': 'Основные',
        'group_id': 1,
        'dictionary_id': 42,
        'category_dependent': True,
        'complex_is_collection': False,
    }, {
        'id': 3,
        'attribute_complex_id': 0,
        'name': 'Нужен код маркировки',
        'description': 'Выберите Да или Нет',
        'type': 'Boolean',
        'is_collection': False,
        'is_required': True,
        'is_aspect': False,
        'max_value_count': 1,
        'group_name': 'Основные',
        'group_id': 1,
        'dictionary_id': 0,
        'category_dependent': True,
        'complex_is_collection': False,
    }])
    return OzonCategoryAttributeSnapshot.objects.create(
        account=account,
        description_category_id=101,
        type_id=202,
        language='DEFAULT',
        schema_hash='f' * 64,
        attributes=attributes,
        attribute_count=len(attributes),
        required_attribute_count=len(attributes),
    )


def _autofill_dictionary_value(account, schema, attribute_id, query, value_id):
    return OzonAttributeValueSnapshot.objects.create(
        account=account,
        description_category_id=101,
        type_id=202,
        attribute_id=attribute_id,
        language='DEFAULT',
        query=query,
        attribute_schema_hash=schema.schema_hash,
        schema_hash=f'{value_id:064x}',
        values=[{'id': value_id, 'value': query, 'info': '', 'picture': ''}],
        value_count=1,
    )


def _complete_product(product):
    ProductPhysicalProfile.objects.create(
        tenant=product.tenant,
        product=product,
        source_barcode='4600000000000',
        source_length_mm=Decimal('100'),
        source_width_mm=Decimal('80'),
        source_height_mm=Decimal('50'),
        source_weight_g=Decimal('500'),
        source_vat_rate=Decimal('20'),
    )
    ProductImage.objects.create(
        product=product,
        s3_key='products/test.jpg',
        sha256='a' * 64,
    )


def _request(client, key, method, product, payload=None, account_id=None):
    url = f'/api/v1/products/{product.pk}/ozon-offer/'
    if account_id is not None:
        url += f'?account_id={account_id}'
    return getattr(client, method)(
        url,
        payload or {},
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {key}',
    )


def _ready_offer(client, key, product, account):
    schema = _attribute_catalog(account)
    _value_snapshot(account, schema)
    _complete_product(product)
    _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })
    return _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'attributes': [{
            'id': 85,
            'complex_id': 0,
            'values': [{
                'value': 'Ignored browser text',
                'dictionary_value_id': 501,
            }],
        }],
    })


@pytest.mark.django_db
def test_offer_identity_is_stable_account_scoped_and_never_enters_avito_runtime():
    tenant, key = _tenant('ozon-offer')
    account = _account(tenant, 'client-1')
    product = _product(tenant)
    _catalog(account)
    client = Client()

    empty = _request(client, key, 'get', product, account_id=account.pk)
    started = _request(client, key, 'patch', product, {'account_id': account.pk})
    selected = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })

    assert empty.status_code == started.status_code == selected.status_code == 200
    assert empty.json()['data']['draft'] is None
    offer_id = started.json()['data']['draft']['offer_id']
    assert selected.json()['data']['draft']['offer_id'] == offer_id
    account.name = 'Переименованный кабинет'
    account.save(update_fields=['name', 'updated_at'])
    reread = _request(client, key, 'get', product, account_id=account.pk)
    assert reread.json()['data']['draft']['offer_id'] == offer_id
    assert OzonOfferDraft.objects.get().category_path == 'Автотовары'
    assert Listing.objects.count() == MarketplaceFeedRun.objects.count() == 0


@pytest.mark.django_db
def test_ozon_autofill_uses_safe_facts_and_recommends_regulatory_review():
    tenant, key = _tenant('ozon-autofill')
    account = _account(tenant, 'client-autofill')
    product = _product(tenant)
    _catalog(account)
    schema = _autofill_attribute_catalog(account)
    _autofill_dictionary_value(account, schema, 4, 'Test Brand', 401)
    _autofill_dictionary_value(account, schema, 6, 'Амортизатор', 601)
    client = Client()
    selected = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })

    response = _request(client, key, 'post', product, {
        'account_id': account.pk,
    })

    assert selected.status_code == response.status_code == 200
    data = response.json()['data']
    values = {
        attribute['id']: attribute['selected_values'][0]
        for attribute in data['attributes']
        if attribute['selected_values']
    }
    assert values == {
        1: {'value': 'Test Brand OZ-1', 'dictionary_value_id': 0},
        4: {'value': 'Test Brand', 'dictionary_value_id': 401},
        5: {'value': 'OZ-1', 'dictionary_value_id': 0},
        6: {'value': 'Амортизатор', 'dictionary_value_id': 601},
    }
    assert data['autofill']['status'] == 'needs_review'
    assert data['autofill']['applied_count'] == 4
    assert {
        item['code'] for item in data['autofill']['recommendations']
    } == {
        'tnved_confirmation_required',
        'marking_confirmation_required',
    }
    assert {
        item['code'] for item in data['preflight']['errors']
    } >= {'required_attribute_missing'}
    assert Listing.objects.count() == MarketplaceFeedRun.objects.count() == 0


@pytest.mark.django_db
def test_ozon_autofill_never_overwrites_tenant_confirmed_value():
    tenant, key = _tenant('ozon-autofill-manual')
    account = _account(tenant, 'client-autofill-manual')
    product = _product(tenant)
    _catalog(account)
    schema = _autofill_attribute_catalog(account)
    _autofill_dictionary_value(account, schema, 4, 'Test Brand', 401)
    _autofill_dictionary_value(account, schema, 6, 'Амортизатор', 601)
    client = Client()
    _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })
    autofilled = _request(client, key, 'post', product, {
        'account_id': account.pk,
    }).json()['data']
    payload = []
    for attribute in autofilled['attributes']:
        values = attribute['selected_values']
        if attribute['id'] == 5:
            values = [{'value': 'MANUAL-PART', 'dictionary_value_id': 0}]
        if values:
            payload.append({
                'id': attribute['id'],
                'complex_id': attribute['complex_id'],
                'values': values,
            })
    saved = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'attributes': payload,
    })
    product.article = 'NEW-ARTICLE'
    product.save(update_fields=['article', 'updated_at'])

    repeated = _request(client, key, 'post', product, {
        'account_id': account.pk,
    })

    assert saved.status_code == repeated.status_code == 200
    part_number = next(
        item for item in repeated.json()['data']['attributes'] if item['id'] == 5
    )
    assert part_number['selected_values'] == [{
        'value': 'MANUAL-PART',
        'dictionary_value_id': 0,
    }]
    assert repeated.json()['data']['autofill']['fields']['0:5']['state'] == 'kept_manual'


@pytest.mark.django_db
def test_connected_ozon_account_schedules_durable_autofill_after_enrichment():
    tenant, _key = _tenant('ozon-autofill-dispatch')
    _account(tenant, 'client-autofill-dispatch')
    product = _product(tenant)

    with patch('apps.core.dispatch.enqueue_durable_task') as enqueue:
        scheduled = schedule_ozon_autofill(
            product.pk,
            trigger_key='parse-job:17',
        )

    assert scheduled is True
    enqueue.assert_called_once_with(
        'apps.marketplaces.tasks.prepare_ozon_offers_after_enrichment',
        args=[product.pk],
        deduplication_key='ozon-autofill:parse-job:17',
        max_run_attempts=4,
    )


@pytest.mark.django_db
def test_offer_identity_is_separate_for_two_ozon_accounts_of_one_tenant():
    tenant, key = _tenant('ozon-multi-account')
    first = _account(tenant, 'client-first')
    second = _account(tenant, 'client-second')
    product = _product(tenant)
    _catalog(first)
    _catalog(second)
    client = Client()

    first_response = _request(client, key, 'patch', product, {'account_id': first.pk})
    second_response = _request(client, key, 'patch', product, {'account_id': second.pk})

    assert first_response.status_code == second_response.status_code == 200
    first_draft = first_response.json()['data']['draft']
    second_draft = second_response.json()['data']['draft']
    assert first_draft['offer_id'] != second_draft['offer_id']
    assert OzonOfferDraft.objects.filter(tenant=tenant, product=product).count() == 2
    assert Listing.objects.count() == MarketplaceFeedRun.objects.count() == 0


@pytest.mark.django_db
def test_offer_api_fences_tenant_and_provider_and_rejects_non_leaf_category():
    tenant, key = _tenant('ozon-fence')
    other, other_key = _tenant('ozon-fence-other')
    account = _account(tenant, 'client-fence')
    avito = _account(tenant, 'avito-fence', marketplace='avito')
    product = _product(tenant)
    _catalog(account)

    foreign_product = _request(Client(), other_key, 'patch', product, {'account_id': account.pk})
    wrong_account = _request(Client(), key, 'patch', product, {'account_id': avito.pk})
    invalid_leaf = _request(Client(), key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 999,
    })

    assert foreign_product.status_code == wrong_account.status_code == 404
    assert invalid_leaf.status_code == 400
    assert invalid_leaf.json()['code'] == 'invalid_category_type'
    assert not OzonOfferDraft.objects.exists()
    assert other.products.count() == 0


@pytest.mark.django_db
def test_preflight_is_ready_only_after_current_required_dictionary_value():
    tenant, key = _tenant('ozon-preflight')
    account = _account(tenant, 'client-preflight')
    product = _product(tenant)
    _catalog(account)
    schema = _attribute_catalog(account)
    _value_snapshot(account, schema)
    _complete_product(product)
    client = Client()

    category = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })
    codes = {
        item['code']
        for item in category.json()['data']['preflight']['errors']
    }
    assert codes == {'attribute_schema_outdated', 'required_attribute_missing'}

    ready = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'attributes': [{
            'id': 85,
            'complex_id': 0,
            'values': [{
                'value': 'Untrusted browser text',
                'dictionary_value_id': 501,
            }],
        }],
    })
    assert ready.status_code == 200
    assert ready.json()['data']['preflight'] == {
        'ready': True,
        'errors': [],
        'recommendations': [],
    }
    draft = OzonOfferDraft.objects.get()
    assert draft.attribute_schema_revision == schema.schema_hash
    assert draft.attributes[0]['values'] == [{
        'value': 'Canonical Brand',
        'dictionary_value_id': 501,
    }]


@pytest.mark.django_db
def test_missing_vat_is_a_recommendation_and_does_not_block_ozon_readiness():
    tenant, key = _tenant('ozon-vat-optional')
    account = _account(tenant, 'client-vat-optional')
    product = _product(tenant)
    _catalog(account)
    schema = _attribute_catalog(account)
    _value_snapshot(account, schema)
    _complete_product(product)
    profile = product.physical_profile
    profile.source_vat_rate = None
    profile.save(update_fields=['source_vat_rate', 'updated_at'])
    client = Client()

    _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })
    response = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'attributes': [{
            'id': 85,
            'complex_id': 0,
            'values': [{
                'value': 'Ignored browser text',
                'dictionary_value_id': 501,
            }],
        }],
    })

    preflight = response.json()['data']['preflight']
    assert response.status_code == 200
    assert preflight['ready'] is True
    assert preflight['errors'] == []
    assert [item['code'] for item in preflight['recommendations']] == [
        'vat_recommended',
    ]


@pytest.mark.django_db
def test_offer_uses_inherited_ozon_margin_without_mutating_avito_runtime():
    tenant, key = _tenant('ozon-offer-price')
    account = _account(tenant, 'client-offer-price')
    second_account = _account(tenant, 'client-offer-price-second')
    product = _product(tenant)
    tree = _catalog(account)
    _catalog(second_account)
    OzonCategoryPolicy.objects.create(
        tenant=tenant,
        account=account,
        description_category_id=101,
        enabled_override=True,
        margin_pct=Decimal('12.50'),
        category_path='Автотовары',
        node_name='Автотовары',
        tree_revision=tree.schema_hash,
    )

    response = _ready_offer(Client(), key, product, account)
    second_schema = _attribute_catalog(second_account)
    _value_snapshot(second_account, second_schema)
    client = Client()
    _request(client, key, 'patch', product, {
        'account_id': second_account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })
    second_response = _request(client, key, 'patch', product, {
        'account_id': second_account.pk,
        'attributes': [{
            'id': 85,
            'complex_id': 0,
            'values': [{
                'value': 'Ignored browser text',
                'dictionary_value_id': 501,
            }],
        }],
    })

    assert response.status_code == 200
    data = response.json()['data']
    assert data['pricing']['base_price'] == '1000.00'
    assert data['pricing']['effective_margin_pct'] == '12.50'
    assert data['pricing']['final_price'] == '1125.00'
    assert data['pricing']['policy']['effective_enabled'] is True
    assert data['pricing']['policy']['margin_source']['description_category_id'] == 101
    assert data['preflight']['ready'] is True
    second_data = second_response.json()['data']
    assert second_data['pricing']['effective_margin_pct'] == '0'
    assert second_data['pricing']['final_price'] == '1000.00'
    assert second_data['preflight']['ready'] is True
    assert OzonOfferDraft.objects.filter(product=product).count() == 2
    assert Listing.objects.count() == MarketplaceFeedRun.objects.count() == 0


@pytest.mark.django_db
def test_offer_margin_override_is_account_scoped_and_can_restore_inheritance():
    tenant, key = _tenant('ozon-offer-margin-override')
    account = _account(tenant, 'client-offer-margin-override')
    product = _product(tenant)
    tree = _catalog(account)
    OzonCategoryPolicy.objects.create(
        tenant=tenant,
        account=account,
        description_category_id=101,
        enabled_override=True,
        margin_pct=Decimal('12.50'),
        category_path='Автотовары',
        node_name='Автотовары',
        tree_revision=tree.schema_hash,
    )
    client = Client()
    _ready_offer(client, key, product, account)

    overridden = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'margin_pct': '25.00',
    })
    assert overridden.status_code == 200
    override_data = overridden.json()['data']
    assert override_data['draft']['margin_pct'] == '25.00'
    assert override_data['pricing']['margin_override'] == '25.00'
    assert override_data['pricing']['margin_source'] == 'offer_margin'
    assert override_data['pricing']['final_price'] == '1250.00'
    exact = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'margin_pct': None,
        'price_override': '1035.11',
    })
    exact_data = exact.json()['data']
    assert exact.status_code == 200
    assert exact_data['draft']['price_override'] == '1035.11'
    assert exact_data['draft']['margin_pct'] is None
    assert exact_data['pricing']['margin_source'] == 'offer_price'
    assert exact_data['pricing']['final_price'] == '1035.11'
    inherited = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'margin_pct': None,
        'price_override': None,
    })
    assert inherited.status_code == 200
    inherited_data = inherited.json()['data']
    assert inherited_data['draft']['margin_pct'] is None
    assert inherited_data['draft']['price_override'] is None
    assert inherited_data['pricing']['margin_override'] is None
    assert inherited_data['pricing']['effective_margin_pct'] == '12.50'
    assert inherited_data['pricing']['final_price'] == '1125.00'
    assert Listing.objects.count() == MarketplaceFeedRun.objects.count() == 0


@pytest.mark.django_db
def test_disabled_category_and_nonpositive_ozon_price_block_only_ozon():
    tenant, key = _tenant('ozon-offer-policy-block')
    account = _account(tenant, 'client-offer-policy-block')
    product = _product(tenant)
    tree = _catalog(account)
    client = Client()
    ready = _ready_offer(client, key, product, account)
    assert ready.json()['data']['preflight']['ready'] is True
    draft = OzonOfferDraft.objects.get(account=account, product=product)
    draft_updated_at = draft.updated_at
    OzonCategoryPolicy.objects.create(
        tenant=tenant,
        account=account,
        description_category_id=101,
        enabled_override=False,
        margin_pct=Decimal('-100.00'),
        category_path='Автотовары',
        node_name='Автотовары',
        tree_revision=tree.schema_hash,
    )

    response = _request(client, key, 'get', product, account_id=account.pk)

    assert response.status_code == 200
    data = response.json()['data']
    assert data['pricing']['final_price'] == '0.00'
    assert data['pricing']['policy']['effective_enabled'] is False
    assert {issue['code'] for issue in data['preflight']['errors']} == {
        'category_disabled',
        'offer_price_invalid',
    }
    draft.refresh_from_db()
    assert draft.updated_at == draft_updated_at
    assert Listing.objects.count() == MarketplaceFeedRun.objects.count() == 0


@pytest.mark.django_db
def test_offer_rejects_arbitrary_or_stale_dictionary_value_ids():
    tenant, key = _tenant('ozon-dictionary-fence')
    account = _account(tenant, 'client-dictionary-fence')
    product = _product(tenant)
    _catalog(account)
    current_schema = _attribute_catalog(account)
    stale_schema_hash = 'd' * 64
    stale_snapshot = _value_snapshot(account, current_schema)
    stale_snapshot.attribute_schema_hash = stale_schema_hash
    stale_snapshot.save(update_fields=['attribute_schema_hash', 'updated_at'])
    client = Client()
    _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })

    stale = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'attributes': [{
            'id': 85,
            'complex_id': 0,
            'values': [{'value': 'Fake', 'dictionary_value_id': 501}],
        }],
    })
    arbitrary = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'attributes': [{
            'id': 85,
            'complex_id': 0,
            'values': [{'value': 'Fake', 'dictionary_value_id': 999}],
        }],
    })

    assert stale.status_code == arbitrary.status_code == 400
    assert stale.json()['code'] == arbitrary.json()['code'] == 'invalid_dictionary_value'
    draft = OzonOfferDraft.objects.get()
    assert draft.attributes == []
    assert draft.attribute_schema_revision == ''


@pytest.mark.django_db
def test_offer_accepts_only_boolean_choices_and_canonical_dictionary_values():
    tenant, key = _tenant('ozon-strict-values')
    account = _account(tenant, 'client-strict-values')
    product = _product(tenant)
    _catalog(account)
    schema = _strict_input_attribute_catalog(account)
    _autofill_dictionary_value(account, schema, 2, '4009 32 000 0', 932)
    client = Client()
    _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })

    invalid_boolean = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'attributes': [{
            'id': 2,
            'complex_id': 0,
            'values': [{'value': 'ignored', 'dictionary_value_id': 932}],
        }, {
            'id': 3,
            'complex_id': 0,
            'values': [{'value': '4009 32 000 0', 'dictionary_value_id': 0}],
        }],
    })
    invalid_dictionary = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'attributes': [{
            'id': 2,
            'complex_id': 0,
            'values': [{'value': 'false', 'dictionary_value_id': 0}],
        }],
    })
    multiple_boolean_values = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'attributes': [{
            'id': 3,
            'complex_id': 0,
            'values': [
                {'value': 'false', 'dictionary_value_id': 0},
                {'value': 'true', 'dictionary_value_id': 0},
            ],
        }],
    })
    valid = _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'attributes': [{
            'id': 2,
            'complex_id': 0,
            'values': [{'value': 'ignored', 'dictionary_value_id': 932}],
        }, {
            'id': 3,
            'complex_id': 0,
            'values': [{'value': 'false', 'dictionary_value_id': 0}],
        }],
    })

    assert (
        invalid_boolean.status_code
        == invalid_dictionary.status_code
        == multiple_boolean_values.status_code
        == 400
    )
    assert invalid_boolean.json()['code'] == 'invalid_boolean_value'
    assert invalid_dictionary.json()['code'] == 'invalid_dictionary_value'
    assert multiple_boolean_values.json()['code'] == 'invalid_boolean_value'
    assert valid.status_code == 200
    assert OzonOfferDraft.objects.get().attributes == [{
        'id': 2,
        'complex_id': 0,
        'values': [{
            'value': '4009 32 000 0',
            'dictionary_value_id': 932,
        }],
    }, {
        'id': 3,
        'complex_id': 0,
        'values': [{'value': 'false', 'dictionary_value_id': 0}],
    }]


@pytest.mark.django_db
def test_preflight_blocks_legacy_invalid_ozon_attribute_values():
    tenant, key = _tenant('ozon-legacy-invalid-values')
    account = _account(tenant, 'client-legacy-invalid-values')
    product = _product(tenant)
    _catalog(account)
    schema = _strict_input_attribute_catalog(account)
    client = Client()
    _request(client, key, 'patch', product, {
        'account_id': account.pk,
        'description_category_id': 101,
        'type_id': 202,
    })
    draft = OzonOfferDraft.objects.get()
    draft.attributes = [{
        'id': 2,
        'complex_id': 0,
        'values': [{'value': 'false', 'dictionary_value_id': 0}],
    }, {
        'id': 3,
        'complex_id': 0,
        'values': [{'value': '4009 32 000 0', 'dictionary_value_id': 0}],
    }]
    draft.attribute_schema_revision = schema.schema_hash
    draft.save(update_fields=['attributes', 'attribute_schema_revision', 'updated_at'])

    response = _request(client, key, 'get', product, account_id=account.pk)

    invalid = [
        issue for issue in response.json()['data']['preflight']['errors']
        if issue['code'] == 'invalid_attribute_value'
    ]
    assert response.status_code == 200
    assert {issue['label'] for issue in invalid} == {
        'ТН ВЭД коды ЕАЭС',
        'Нужен код маркировки',
    }


@pytest.mark.django_db
def test_dictionary_search_is_explicit_versioned_and_account_fenced(settings):
    tenant, _key = _tenant('ozon-values')
    other, _other_key = _tenant('ozon-values-other')
    account = _account(tenant, 'client-values')
    _catalog(account)
    schema = _attribute_catalog(account)
    settings.OZON_ACCOUNT_CONNECTION_ENABLED = True
    settings.OZON_ACCOUNT_CONNECTION_TENANT_SLUGS = (tenant.slug,)
    settings.OZON_ACCOUNT_CONNECTION_CLIENT_IDS = (account.external_id,)
    owner_token = owner_access_token(tenant)
    other_owner_token = owner_access_token(other)
    url = f'/api/v1/accounts/{account.pk}/ozon-catalog/attribute-values/search/'
    payload = {
        'description_category_id': 101,
        'type_id': 202,
        'attribute_id': 85,
        'query': 'Test',
        'language': 'DEFAULT',
        'confirm_ozon_read_only_access': True,
    }

    with patch(VALUE_SEARCH, return_value=[
        {'id': 502, 'value': 'Second', 'info': '', 'picture': ''},
        {'id': 501, 'value': 'First', 'info': '', 'picture': ''},
        {'id': 502, 'value': 'Duplicate', 'info': '', 'picture': ''},
    ]) as provider_read:
        response = Client().post(
            url,
            payload,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {owner_token}',
        )
        foreign = Client().post(
            url,
            payload,
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {other_owner_token}',
        )

    assert response.status_code == 200
    assert [item['id'] for item in response.json()['data']['values']] == [502, 501]
    assert foreign.status_code == 404
    provider_read.assert_called_once_with(
        description_category_id=101,
        type_id=202,
        attribute_id=85,
        value='Test',
    )
    snapshot = OzonAttributeValueSnapshot.objects.get()
    assert snapshot.attribute_schema_hash == schema.schema_hash
    assert snapshot.schema_hash
    assert not OzonOfferDraft.objects.exists()
    assert other.marketplace_accounts.count() == 0
