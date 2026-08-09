from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.tenants.models import APIKey, TenantUser
from apps.tenants.services import APIKeyService, TenantService

User = get_user_model()


@pytest.mark.django_db
class TestTenantService:
    def test_create_tenant_creates_owner_role(self):
        """При создании тенанта пользователю присваивается роль owner."""
        tenant, _ = TenantService.create_tenant(
            name='Test Co', slug='test-co',
            owner_email='owner@test.com', owner_password='pass12345',
        )
        membership = TenantUser.objects.get(tenant=tenant, user__email='owner@test.com')
        assert membership.role == TenantUser.ROLE_OWNER

    def test_create_tenant_default_catalog_domain_is_unknown(self):
        """При создании тенанта дефолтный домен каталога — 'unknown' (Не определено)."""
        tenant, _ = TenantService.create_tenant(
            name='Catalog Co', slug='catalog-co',
            owner_email='catalog@test.com', owner_password='pass12345',
        )

        assert tenant.catalog_domain == 'unknown'

    def test_create_tenant_returns_api_key(self):
        """При создании тенанта возвращается plaintext API Key."""
        _, plaintext = TenantService.create_tenant(
            name='Key Co', slug='key-co',
            owner_email='key@test.com', owner_password='pass12345',
        )
        assert plaintext.startswith(APIKey.KEY_PREFIX)

    def test_registration_key_is_short_lived_and_read_only(self):
        tenant, _ = TenantService.create_tenant(
            name='Minimal Co', slug='minimal-co',
            owner_email='minimal@test.com', owner_password='pass12345',
        )

        api_key = APIKey.objects.get(tenant=tenant)
        assert api_key.role == APIKey.ROLE_VIEWER
        assert api_key.scopes == ['tenant:read']
        assert api_key.expires_at <= timezone.now() + timedelta(hours=25)

    def test_api_key_hash_stored_not_plaintext(self):
        """В БД хранится хэш ключа, а не plaintext."""
        tenant, plaintext = TenantService.create_tenant(
            name='Hash Co', slug='hash-co',
            owner_email='hash@test.com', owner_password='pass12345',
        )
        api_key = APIKey.objects.get(tenant=tenant)
        assert api_key.key_hash != plaintext
        assert len(api_key.key_hash) == 64   # SHA256 hex

    def test_add_user_creates_membership(self):
        """add_user создаёт пользователя и членство в тенанте."""
        tenant, _ = TenantService.create_tenant(
            name='Team Co', slug='team-co',
            owner_email='boss@team.com', owner_password='pass12345',
        )
        membership = TenantService.add_user(tenant, 'worker@team.com', TenantUser.ROLE_OPERATOR)
        assert membership.role == TenantUser.ROLE_OPERATOR
        assert membership.user.email == 'worker@team.com'

    def test_remove_owner_raises_error(self):
        """Нельзя удалить владельца тенанта."""
        tenant, _ = TenantService.create_tenant(
            name='Owner Co', slug='owner-co',
            owner_email='owner2@test.com', owner_password='pass12345',
        )
        with pytest.raises(ValueError, match='Нельзя удалить владельца'):
            TenantService.remove_user(tenant, 'owner2@test.com')


@pytest.mark.django_db
class TestAPIKeyAuthentication:
    def test_api_key_authentication_works(self):
        """Верный ключ возвращает объект APIKey."""
        tenant, plaintext = TenantService.create_tenant(
            name='Auth Co', slug='auth-co',
            owner_email='auth@test.com', owner_password='pass12345',
        )
        verified = APIKey.verify(plaintext)
        assert verified is not None
        assert verified.tenant_id == tenant.pk

    def test_wrong_key_returns_none(self):
        """Неверный ключ возвращает None."""
        result = APIKey.verify('map_sk_wrong_key_totally_fake')
        assert result is None

    def test_revoked_key_not_verified(self):
        """Отозванный ключ не проходит верификацию."""
        tenant, plaintext = TenantService.create_tenant(
            name='Revoke Co', slug='revoke-co',
            owner_email='revoke@test.com', owner_password='pass12345',
        )
        api_key = APIKey.objects.get(tenant=tenant)
        APIKeyService.revoke_key(api_key.pk, tenant)
        assert APIKey.verify(plaintext) is None


@pytest.mark.django_db
class TestTenantIsolation:
    def test_tenant_isolation(self):
        """Запрос от тенанта A не видит данные тенанта B."""
        tenant_a, _ = TenantService.create_tenant(
            name='Company A', slug='company-a',
            owner_email='a@test.com', owner_password='pass12345',
        )
        tenant_b, _ = TenantService.create_tenant(
            name='Company B', slug='company-b',
            owner_email='b@test.com', owner_password='pass12345',
        )
        # Ключи тенанта A не дают доступ к данным тенанта B
        keys_a = APIKey.objects.filter(tenant=tenant_a)
        keys_b = APIKey.objects.filter(tenant=tenant_b)
        assert not keys_a.filter(tenant=tenant_b).exists()
        assert not keys_b.filter(tenant=tenant_a).exists()
