"""
Тесты PATCH /api/v1/listings/{id}/.

Покрывают: атомарность сохранения (ошибка на одном из шагов не должна
оставлять частично применённые изменения) и текст ошибки в ответе.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import Client

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplacePlacementAddress
from apps.marketplaces.price_utils import compute_price, effective_margin
from apps.marketplaces.services import ListingService
from apps.products.models import Product, TenantCatalogCategory
from apps.tenants.tests.auth import create_tenant_with_operator_key


def make_tenant(slug):
    return create_tenant_with_operator_key(
        slug, slug, f'{slug}@test.com', 'pass12345',
    )


def make_account(tenant):
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Test Account',
        external_id='12345',
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'csec'}),
        default_address='Москва, Тверская улица, 1',
        default_manager_name='Менеджер',
        default_contact_phone='+79990000000',
    )


def make_product(tenant):
    return Product.objects.create(
        tenant=tenant,
        article='ART-001',
        name='Фонарь левый',
        brand='Jorden',
        price=Decimal('3000'),
        stock_qty=5,
        category_1c='Оптика',
        condition='new',
    )


def make_listing(tenant, product, account, **kwargs):
    kwargs.setdefault('status', Listing.STATUS_ARCHIVED)
    return Listing.objects.create(
        tenant=tenant, product=product, account=account,
        price_on_listing=Decimal('3000'),
        title='Фонарь левый Jorden',
        description_ai='Описание тестовое',
        **kwargs,
    )


@pytest.mark.django_db
class TestListingPatchAPI:
    def test_margin_not_saved_when_placement_step_fails(self):
        """
        Регрессия: раньше маржа коммитилась отдельным вызовом до того, как
        шаг с адресом падал с ListingNotFound — итог: в БД цена менялась,
        а фронту приходила ошибка. Теперь весь patch() атомарен.
        """
        tenant, key = make_tenant('listing-patch-atomic-co')
        account = make_account(tenant)
        address = MarketplacePlacementAddress.objects.create(
            tenant=tenant, account=account, name='Москва', address='Москва',
        )
        product = make_product(tenant)
        listing = make_listing(
            tenant, product, account, margin_pct=Decimal('10.00'), placement_address=address,
        )

        # Деактивируем адрес — как в проде, где адрес выключили руками.
        address.is_active = False
        address.save(update_fields=['is_active'])

        c = Client()
        resp = c.patch(
            f'/api/v1/listings/{listing.pk}/',
            data={
                'margin_pct': '35.00',
                'placement_address': address.pk,
            },
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
        )

        assert resp.status_code == 404
        body = resp.json()
        assert body['message'] == 'Адрес размещения не найден'

        listing.refresh_from_db()
        assert listing.margin_pct == Decimal('10.00')

    def test_returns_message_for_not_found(self):
        tenant, key = make_tenant('listing-patch-404-co')
        c = Client()
        resp = c.patch(
            '/api/v1/listings/999999/',
            data={'title': 'x'},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
        )
        assert resp.status_code == 404
        assert resp.json()['message']

    def test_save_returns_field_errors_without_discarding_draft_changes(self):
        tenant, key = make_tenant('listing-save-validation-co')
        account = make_account(tenant)
        account.default_manager_name = ''
        account.default_contact_phone = ''
        account.save(update_fields=['default_manager_name', 'default_contact_phone'])
        product = make_product(tenant)
        listing = make_listing(tenant, product, account)

        c = Client()
        resp = c.patch(
            f'/api/v1/listings/{listing.pk}/',
            data={'title': 'Сохранённый заголовок'},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
        )

        assert resp.status_code == 200
        body = resp.json()['data']
        assert body['title'] == 'Сохранённый заголовок'
        assert set(body['avito_field_errors']) >= {
            'manager_name_override',
            'contact_phone_override',
        }
        assert set(body['avito_field_warnings_by_field']) >= {
            'catalog_category',
            'images',
        }
        listing.refresh_from_db()
        assert listing.title == 'Сохранённый заголовок'

    def test_save_marks_missing_required_placement_as_field_error(self):
        tenant, key = make_tenant('listing-save-placement-validation-co')
        account = make_account(tenant)
        account.default_address = ''
        account.save(update_fields=['default_address'])
        listing = make_listing(tenant, make_product(tenant), account)

        c = Client()
        resp = c.patch(
            f'/api/v1/listings/{listing.pk}/',
            data={'title': 'Проверка размещения'},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
        )

        assert resp.status_code == 200
        errors = resp.json()['data']['avito_field_errors']
        assert 'placement_address' in errors
        assert 'адрес' in errors['placement_address'][0].lower()

    def test_publish_validation_preserves_terminal_state_and_previous_reason(self):
        tenant, key = make_tenant('listing-publish-validation-co')
        account = make_account(tenant)
        product = make_product(tenant)
        category = TenantCatalogCategory.objects.create(
            tenant=tenant,
            name='Поперечные дуги и комплектующие',
            normalized_name='поперечныедугиикомплектующие',
            domain=TenantCatalogCategory.Domain.AUTO_PARTS,
            external_source='avito',
            external_id='poperechnye_dugi_i_komplektuyushie',
        )
        product.catalog_category = category
        product.brand = ''
        product.save(update_fields=['brand', 'catalog_category'])
        listing = make_listing(
            tenant,
            product,
            account,
            rejection_reason='Результат прошлой попытки',
        )

        c = Client()
        resp = c.post(
            f'/api/v1/listings/{listing.pk}/publish/',
            HTTP_AUTHORIZATION=f'Bearer {key}',
        )

        assert resp.status_code == 400
        body = resp.json()
        assert body['code'] == 'listing_validation_error'
        assert 'product_brand' in body['field_errors']
        listing.refresh_from_db()
        assert listing.status == Listing.STATUS_ARCHIVED
        assert listing.rejection_reason == 'Результат прошлой попытки'

    def test_active_listing_accepts_price_only_and_enqueues_safe_update(
        self, django_capture_on_commit_callbacks,
    ):
        tenant, key = make_tenant('listing-active-price-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(
            tenant,
            product,
            account,
            status=Listing.STATUS_ACTIVE,
            external_id='avito-item-42',
        )

        c = Client()
        with patch('apps.marketplaces.services._enqueue_price_update') as enqueue:
            with django_capture_on_commit_callbacks(execute=True):
                resp = c.patch(
                    f'/api/v1/listings/{listing.pk}/',
                    data={'price_on_listing': '4250.00'},
                    content_type='application/json',
                    HTTP_AUTHORIZATION=f'Bearer {key}',
                )

        assert resp.status_code == 200
        listing.refresh_from_db()
        assert listing.price_on_listing == Decimal('4250.00')
        enqueue.assert_called_once_with(listing.pk)

    def test_active_listing_rejects_price_with_content_change(self):
        tenant, key = make_tenant('listing-active-content-co')
        account = make_account(tenant)
        product = make_product(tenant)
        listing = make_listing(
            tenant,
            product,
            account,
            status=Listing.STATUS_ACTIVE,
            external_id='avito-item-43',
        )

        c = Client()
        resp = c.patch(
            f'/api/v1/listings/{listing.pk}/',
            data={'price_on_listing': '4250.00', 'title': 'Новый заголовок'},
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {key}',
        )

        assert resp.status_code == 400
        listing.refresh_from_db()
        assert listing.price_on_listing == Decimal('3000')
        assert listing.title == 'Фонарь левый Jorden'


class TestComputePriceRounding:
    def test_rounds_up_to_whole_ruble(self):
        assert compute_price(Decimal('3475.11'), Decimal('0')) == Decimal('3476')


@pytest.mark.django_db
def test_listing_inherits_margin_from_nearest_category_parent():
    tenant, _ = make_tenant('inherited-margin-co')
    account = make_account(tenant)
    parent = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Двигатель',
        default_margin_pct=Decimal('15.00'),
    )
    child = TenantCatalogCategory.objects.create(
        tenant=tenant,
        name='Головка блока цилиндров',
        parent=parent,
        default_margin_pct=None,
    )
    product = make_product(tenant)
    product.catalog_category = child
    product.price = Decimal('1000.00')
    product.save(update_fields=['catalog_category', 'price'])

    listing = ListingService.create_or_update(product, account, auto_publish=False)

    assert listing.price_on_listing == Decimal('1150')
    assert effective_margin(listing) == Decimal('15.00')

    child.default_margin_pct = Decimal('0')
    child.save(update_fields=['default_margin_pct'])
    listing.refresh_from_db()
    assert effective_margin(listing) == Decimal('0')

    def test_no_rounding_needed_when_already_whole(self):
        assert compute_price(Decimal('100'), Decimal('0')) == Decimal('100')

    def test_rounds_up_fractional_margin_result(self):
        assert compute_price(Decimal('3000'), Decimal('15.8367')) == Decimal('3476')
