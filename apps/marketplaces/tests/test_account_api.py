"""
Тесты API эндпоинта MarketplaceAccount.

Покрывают: создание, чтение, обновление, удаление аккаунта,
изоляцию тенантов, защиту credentials.
"""
import pytest

from apps.datasources.encryption import decrypt
from apps.marketplaces.models import MarketplaceAccount


def make_tenant(slug):
    from apps.tenants.services import TenantService
    tenant, plaintext = TenantService.create_tenant(slug, slug, f'{slug}@t.com', 'pass')
    return tenant, plaintext


@pytest.mark.django_db
class TestMarketplaceAccountAPI:
    def test_create_account(self):
        """POST /api/v1/accounts/ создаёт аккаунт и шифрует credentials."""
        from django.test import Client
        tenant, key = make_tenant('acc-create')
        c = Client()

        resp = c.post('/api/v1/accounts/', {
            'name': 'Основной',
            'marketplace': 'avito',
            'external_id': '111222',
            'client_id': 'cid',
            'client_secret': 'csec',
        }, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}')

        assert resp.status_code == 201
        data = resp.json()
        assert data['name'] == 'Основной'
        assert data['external_id'] == '111222'
        assert 'client_id' not in data
        assert 'client_secret' not in data
        assert 'credentials_enc' not in data

    def test_credentials_encrypted_in_db(self):
        """credentials_enc хранится зашифрованным, plaintext не читается как JSON."""
        from django.test import Client
        tenant, key = make_tenant('acc-enc')
        c = Client()

        c.post('/api/v1/accounts/', {
            'name': 'Enc Test',
            'marketplace': 'avito',
            'external_id': '999',
            'client_id': 'my_client_id',
            'client_secret': 'my_secret',
        }, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}')

        account = MarketplaceAccount.objects.get(tenant=tenant)
        decrypted = decrypt(bytes(account.credentials_enc))
        assert decrypted['client_id'] == 'my_client_id'
        assert decrypted['client_secret'] == 'my_secret'
        assert b'my_client_id' not in bytes(account.credentials_enc)

    def test_list_accounts(self):
        """GET /api/v1/accounts/ возвращает только аккаунты своего тенанта."""
        from django.test import Client
        t1, k1 = make_tenant('acc-list-1')
        t2, k2 = make_tenant('acc-list-2')

        for tenant, key in [(t1, k1), (t2, k2)]:
            Client().post('/api/v1/accounts/', {
                'name': 'Acc',
                'marketplace': 'avito',
                'external_id': tenant.slug,
                'client_id': 'x',
                'client_secret': 'y',
            }, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}')

        resp = Client().get('/api/v1/accounts/', HTTP_AUTHORIZATION=f'Bearer {k1}')
        assert resp.status_code == 200
        ids = [a['external_id'] for a in resp.json()]
        assert t1.slug in ids
        assert t2.slug not in ids

    def test_get_account_detail(self):
        """GET /api/v1/accounts/{id}/ возвращает аккаунт."""
        from django.test import Client
        tenant, key = make_tenant('acc-detail')
        c = Client()
        create_resp = c.post('/api/v1/accounts/', {
            'name': 'Detail',
            'marketplace': 'avito',
            'external_id': '321',
            'client_id': 'x',
            'client_secret': 'y',
        }, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}')

        pk = create_resp.json()['id']
        resp = c.get(f'/api/v1/accounts/{pk}/', HTTP_AUTHORIZATION=f'Bearer {key}')
        assert resp.status_code == 200
        assert resp.json()['id'] == pk

    def test_update_account(self):
        """PUT /api/v1/accounts/{id}/ обновляет имя и credentials."""
        from django.test import Client
        tenant, key = make_tenant('acc-update')
        c = Client()

        pk = c.post('/api/v1/accounts/', {
            'name': 'Old Name',
            'marketplace': 'avito',
            'external_id': '555',
            'client_id': 'old_id',
            'client_secret': 'old_sec',
        }, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}').json()['id']

        resp = c.put(f'/api/v1/accounts/{pk}/', {
            'name': 'New Name',
            'marketplace': 'avito',
            'external_id': '555',
            'client_id': 'new_id',
            'client_secret': 'new_sec',
        }, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}')

        assert resp.status_code == 200
        assert resp.json()['name'] == 'New Name'
        account = MarketplaceAccount.objects.get(pk=pk)
        assert decrypt(bytes(account.credentials_enc))['client_id'] == 'new_id'

    def test_delete_account(self):
        """DELETE /api/v1/accounts/{id}/ удаляет аккаунт."""
        from django.test import Client
        tenant, key = make_tenant('acc-delete')
        c = Client()

        pk = c.post('/api/v1/accounts/', {
            'name': 'To Delete',
            'marketplace': 'avito',
            'external_id': '777',
            'client_id': 'x',
            'client_secret': 'y',
        }, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}').json()['id']

        resp = c.delete(f'/api/v1/accounts/{pk}/', HTTP_AUTHORIZATION=f'Bearer {key}')
        assert resp.status_code == 204
        assert not MarketplaceAccount.objects.filter(pk=pk).exists()

    def test_cannot_access_other_tenant_account(self):
        """GET чужого аккаунта возвращает 404."""
        from django.test import Client
        t1, k1 = make_tenant('acc-iso-a')
        t2, k2 = make_tenant('acc-iso-b')

        from apps.datasources.encryption import encrypt
        account = MarketplaceAccount.objects.create(
            tenant=t1,
            name='Private',
            external_id='private',
            credentials_enc=encrypt({'client_id': 'x', 'client_secret': 'y'}),
        )

        resp = Client().get(
            f'/api/v1/accounts/{account.pk}/',
            HTTP_AUTHORIZATION=f'Bearer {k2}',
        )
        assert resp.status_code == 404

    def test_duplicate_external_id_returns_409(self):
        """Создание дублирующего external_id возвращает 409."""
        from django.test import Client
        tenant, key = make_tenant('acc-dup')
        c = Client()
        payload = {
            'name': 'First',
            'marketplace': 'avito',
            'external_id': 'same-id',
            'client_id': 'x',
            'client_secret': 'y',
        }
        c.post('/api/v1/accounts/', payload,
               content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}')

        resp = c.post('/api/v1/accounts/', payload,
                      content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}')
        assert resp.status_code == 409
