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

    def test_register_rejects_non_ascii_slug_with_clear_error(self):
        """Кириллический slug не сохраняется и возвращает понятную ошибку."""
        client = Client()
        response = client.post('/api/v1/auth/register/', {
            'name': 'Моя Компания',
            'slug': 'моя-компания',
            'email': 'owner-rus@myco.com',
            'password': 'strongpass123',
        }, content_type='application/json')

        assert response.status_code == 400
        assert response.json()['status'] == 'error'
        assert 'только английские буквы' in str(response.json())


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


@pytest.mark.django_db
class TestTokenObtainView:
    """POST /api/v1/auth/token/ — получение JWT по email/password."""

    def test_valid_credentials_return_tokens_and_tenant(self):
        """Успешный логин возвращает access, refresh и данные тенанта."""
        client = Client()
        TenantService.create_tenant('Corp', 'corp', 'owner@corp.com', 'pass12345')

        response = client.post('/api/v1/auth/token/', {
            'email': 'owner@corp.com',
            'password': 'pass12345',
        }, content_type='application/json')

        assert response.status_code == 200
        data = response.json()
        assert 'access' in data
        assert 'refresh' in data
        assert data['tenant']['slug'] == 'corp'
        assert data['role'] == 'owner'

    def test_invalid_password_returns_401(self):
        """Неверный пароль → 401."""
        client = Client()
        TenantService.create_tenant('Corp2', 'corp2', 'owner2@corp.com', 'pass12345')

        response = client.post('/api/v1/auth/token/', {
            'email': 'owner2@corp.com',
            'password': 'wrongpassword',
        }, content_type='application/json')

        assert response.status_code == 401

    def test_unknown_email_returns_401(self):
        """Несуществующий email → 401."""
        client = Client()
        response = client.post('/api/v1/auth/token/', {
            'email': 'nobody@nowhere.com',
            'password': 'pass12345',
        }, content_type='application/json')

        assert response.status_code == 401

    def test_tenant_slug_selects_correct_tenant(self):
        """Если у пользователя несколько тенантов — tenant_slug выбирает нужный."""
        from django.contrib.auth import get_user_model
        from apps.tenants.models import Tenant, TenantUser

        client = Client()
        User = get_user_model()

        TenantService.create_tenant('First', 'first', 'multi@corp.com', 'pass12345')
        second = Tenant.objects.create(name='Second', slug='second')
        user = User.objects.get(email='multi@corp.com')
        TenantUser.objects.create(user=user, tenant=second, role=TenantUser.ROLE_OPERATOR)

        response = client.post('/api/v1/auth/token/', {
            'email': 'multi@corp.com',
            'password': 'pass12345',
            'tenant_slug': 'second',
        }, content_type='application/json')

        assert response.status_code == 200
        assert response.json()['tenant']['slug'] == 'second'
        assert response.json()['role'] == 'operator'


@pytest.mark.django_db
class TestMeView:
    """GET /api/v1/auth/me/ — данные текущего пользователя по JWT."""

    def _get_token(self, client, email, password):
        """Хелпер: логин и возврат access-токена."""
        resp = client.post('/api/v1/auth/token/', {
            'email': email,
            'password': password,
        }, content_type='application/json')
        return resp.json()['access']

    def test_authenticated_returns_user_and_tenant(self):
        """Валидный JWT → 200 с данными пользователя и тенанта."""
        client = Client()
        TenantService.create_tenant('MeCorp', 'me-corp', 'me@corp.com', 'pass12345')
        token = self._get_token(client, 'me@corp.com', 'pass12345')

        response = client.get('/api/v1/auth/me/', HTTP_AUTHORIZATION=f'Bearer {token}')

        assert response.status_code == 200
        data = response.json()['data']
        assert data['user']['email'] == 'me@corp.com'
        assert data['tenant']['slug'] == 'me-corp'
        assert data['role'] == 'owner'

    def test_unauthenticated_returns_401(self):
        """Запрос без токена → 401."""
        client = Client()
        response = client.get('/api/v1/auth/me/')
        assert response.status_code == 401

    def test_expired_token_returns_401(self):
        """Просроченный/невалидный токен → 401."""
        client = Client()
        response = client.get(
            '/api/v1/auth/me/',
            HTTP_AUTHORIZATION='Bearer invalidtoken',
        )
        assert response.status_code == 401
