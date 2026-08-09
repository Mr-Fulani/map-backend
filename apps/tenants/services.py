from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.tenants.models import APIKey, Tenant, TenantUser, WebhookEndpoint
from apps.tenants.webhook_limits import webhook_endpoint_quota

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

        _, plaintext = APIKey.generate(
            tenant=tenant,
            name='Registration Read-only Key',
            role=APIKey.ROLE_VIEWER,
            scopes=['tenant:read'],
            expires_at=timezone.now() + timedelta(days=1),
            created_by=user,
        )

        from apps.billing.services import BillingService
        BillingService.start_trial(tenant)

        from apps.products.services import ProductCategorySeedService
        if tenant.catalog_domain != Tenant.CatalogDomain.UNKNOWN:
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
    def create_key(
        tenant: Tenant,
        name: str,
        *,
        role: str,
        scopes: list[str],
        expires_at,
        created_by,
    ) -> tuple[APIKey, str]:
        """
        Создаёт новый API Key.

        Возвращает (объект APIKey, plaintext). Plaintext показывается только здесь.
        """
        return APIKey.generate(
            tenant=tenant,
            name=name,
            role=role,
            scopes=scopes,
            expires_at=expires_at,
            created_by=created_by,
        )

    @staticmethod
    def revoke_key(key_id: int, tenant: Tenant, *, revoked_by=None) -> None:
        """Деактивирует API Key. Проверяет принадлежность тенанту."""
        from django.utils import timezone

        APIKey.objects.filter(
            pk=key_id,
            tenant=tenant,
            is_active=True,
        ).update(
            is_active=False,
            revoked_at=timezone.now(),
            revoked_by=revoked_by,
        )


class WebhookEndpointConflict(ValueError):
    """Base class for safe, user-facing webhook registration conflicts."""


class DuplicateWebhookEndpoint(WebhookEndpointConflict):
    """The tenant already owns a non-deleted endpoint with this URL."""


class WebhookEndpointQuotaExceeded(WebhookEndpointConflict):
    """The tenant has exhausted its endpoint allocation."""


class WebhookEndpointService:
    """Concurrency-safe webhook endpoint registration."""

    UNIQUE_CONSTRAINT = 'unique_live_tenant_webhook_url'

    @classmethod
    def create_endpoint(
        cls,
        *,
        tenant: Tenant,
        url: str,
        events: list[str],
    ) -> tuple[WebhookEndpoint, str]:
        plaintext_secret = WebhookEndpoint.generate_secret()
        try:
            with transaction.atomic():
                # A tenant row is the stable lock shared by all endpoint
                # registrations, preventing concurrent quota over-allocation.
                Tenant.objects.select_for_update().only('pk').get(pk=tenant.pk)
                endpoints = WebhookEndpoint.objects.filter(tenant_id=tenant.pk)
                if endpoints.filter(url=url).exists():
                    raise DuplicateWebhookEndpoint(
                        'Этот webhook URL уже зарегистрирован.',
                    )
                quota = webhook_endpoint_quota()
                if endpoints.count() >= quota:
                    raise WebhookEndpointQuotaExceeded(
                        f'Достигнут лимит webhook endpoints ({quota}).',
                    )

                endpoint = WebhookEndpoint(
                    tenant=tenant,
                    url=url,
                    events=events,
                )
                endpoint.set_secret(plaintext_secret)
                endpoint.save(force_insert=True)
                return endpoint, plaintext_secret
        except IntegrityError as exc:
            constraint_name = getattr(
                getattr(exc, '__cause__', None),
                'diag',
                None,
            )
            constraint_name = getattr(constraint_name, 'constraint_name', None)
            if constraint_name == cls.UNIQUE_CONSTRAINT:
                raise DuplicateWebhookEndpoint(
                    'Этот webhook URL уже зарегистрирован.',
                ) from exc
            raise
