from django.contrib.auth import get_user_model
from django.db import transaction

from apps.tenants.models import APIKey, Tenant, TenantUser

User = get_user_model()


class TenantService:
    """Сервис управления тенантами и пользователями."""

    @staticmethod
    @transaction.atomic
    def create_tenant(name: str, slug: str, owner_email: str, owner_password: str) -> tuple[Tenant, str]:
        """
        Создаёт тенанта, пользователя-владельца и первый API Key.

        Возвращает (тенант, plaintext API Key).
        """
        user, _ = User.objects.get_or_create(
            email=owner_email,
            defaults={'is_active': True},
        )
        if _:
            user.set_password(owner_password)
            user.save(update_fields=['password'])

        tenant = Tenant.objects.create(name=name, slug=slug)

        TenantUser.objects.create(
            user=user,
            tenant=tenant,
            role=TenantUser.ROLE_OWNER,
        )

        _, plaintext = APIKey.generate(tenant=tenant, name='Default Key')

        from apps.billing.services import BillingService
        BillingService.start_trial(tenant)

        from apps.products.services import ProductCategorySeedService
        ProductCategorySeedService.enable_tenant_catalog_domain(tenant, tenant.catalog_domain)
        if tenant.catalog_domain == Tenant.CatalogDomain.MIXED:
            ProductCategorySeedService.enable_tenant_catalog_domain(tenant, Tenant.CatalogDomain.AUTO_PARTS)

        return tenant, plaintext

    @staticmethod
    @transaction.atomic
    def add_user(tenant: Tenant, email: str, role: str = TenantUser.ROLE_OPERATOR) -> TenantUser:
        """Добавляет существующего или нового пользователя в тенант."""
        user, _ = User.objects.get_or_create(email=email)
        membership, created = TenantUser.objects.get_or_create(
            user=user,
            tenant=tenant,
            defaults={'role': role},
        )
        if not created:
            membership.role = role
            membership.save(update_fields=['role'])
        return membership

    @staticmethod
    def remove_user(tenant: Tenant, email: str) -> None:
        """Удаляет пользователя из тенанта. Владельца удалить нельзя."""
        membership = TenantUser.objects.get(user__email=email, tenant=tenant)
        if membership.role == TenantUser.ROLE_OWNER:
            raise ValueError('Нельзя удалить владельца тенанта.')
        membership.delete()


class APIKeyService:
    """Сервис управления API-ключами тенанта."""

    @staticmethod
    def create_key(tenant: Tenant, name: str) -> tuple[APIKey, str]:
        """
        Создаёт новый API Key.

        Возвращает (объект APIKey, plaintext). Plaintext показывается только здесь.
        """
        return APIKey.generate(tenant=tenant, name=name)

    @staticmethod
    def revoke_key(key_id: int, tenant: Tenant) -> None:
        """Деактивирует API Key. Проверяет принадлежность тенанту."""
        APIKey.objects.filter(pk=key_id, tenant=tenant).update(is_active=False)
