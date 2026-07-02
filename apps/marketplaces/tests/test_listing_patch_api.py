"""
Тесты PATCH /api/v1/listings/{id}/.

Покрывают: атомарность сохранения (ошибка на одном из шагов не должна
оставлять частично применённые изменения) и текст ошибки в ответе.
"""
from decimal import Decimal

import pytest
from django.test import Client

from apps.datasources.encryption import encrypt
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplacePlacementAddress
from apps.marketplaces.price_utils import compute_price
from apps.products.models import Product
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, key = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant, key


def make_account(tenant):
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Test Account',
        external_id='12345',
        credentials_enc=encrypt({'client_id': 'cid', 'client_secret': 'csec'}),
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


class TestComputePriceRounding:
    def test_rounds_up_to_whole_ruble(self):
        assert compute_price(Decimal('3475.11'), Decimal('0')) == Decimal('3476')

    def test_no_rounding_needed_when_already_whole(self):
        assert compute_price(Decimal('100'), Decimal('0')) == Decimal('100')

    def test_rounds_up_fractional_margin_result(self):
        assert compute_price(Decimal('3000'), Decimal('15.8367')) == Decimal('3476')
