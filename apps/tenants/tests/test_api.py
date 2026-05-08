import pytest
from django.test import Client

from apps.tenants.models import APIKey
from apps.tenants.services import TenantService


@pytest.mark.django_db
class TestRegisterView:
    def test_register_creates_tenant_and_returns_key(self):
        """POST /register/ создаёт тенанта и возвращает API Key."""
        client = Client()
        response = client.post('/api/v1/auth/register/', {
            'name': 'My Company',
            'slug': 'my-company',
            'email': 'owner@myco.com',
            'password': 'strongpass123',
        }, content_type='application/json')

        assert response.status_code == 201
        data = response.json()
        assert data['status'] == 'ok'
        assert data['data']['api_key'].startswith(APIKey.KEY_PREFIX)

    def test_register_duplicate_slug_returns_error(self):
        """Повторная регистрация с тем же slug возвращает ошибку валидации."""
        client = Client()
        TenantService.create_tenant('Existing', 'existing', 'e@e.com', 'pass12345')

        response = client.post('/api/v1/auth/register/', {
            'name': 'Another',
            'slug': 'existing',
            'email': 'other@e.com',
            'password': 'pass12345',
        }, content_type='application/json')

        assert response.status_code == 400
        assert response.json()['status'] == 'error'


@pytest.mark.django_db
class TestTenantDetailView:
    def test_authenticated_request_returns_tenant(self):
        """Запрос с валидным API Key возвращает данные тенанта."""
        client = Client()
        _, plaintext = TenantService.create_tenant(
            'Test Corp', 'test-corp', 'corp@test.com', 'pass12345',
        )
        response = client.get(
            '/api/v1/tenant/',
            HTTP_AUTHORIZATION=f'Bearer {plaintext}',
        )
        assert response.status_code == 200
        assert response.json()['data']['slug'] == 'test-corp'

    def test_unauthenticated_request_returns_401(self):
        """Запрос без API Key возвращает 401."""
        client = Client()
        response = client.get('/api/v1/tenant/')
        assert response.status_code == 401
