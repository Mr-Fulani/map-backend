from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.datasources.encryption import encrypt
from apps.marketplaces.account_errors import (
    AccountAlreadyExists,
    InvalidMarketplaceCredentials,
    MarketplaceConnectionError,
    MarketplaceProviderDisabled,
)
from apps.marketplaces.adapters.ozon.client import OzonAPIError, OzonSellerClient
from apps.marketplaces.models import MarketplaceAccount, OzonAccountProfile
from apps.marketplaces.ozon_rollout import ozon_connection_enabled_for_account


class OzonAccountConnectionService:
    """Connects and rotates Ozon accounts without entering Avito workflows."""

    @staticmethod
    def _verify_connection(data: dict):
        try:
            return OzonSellerClient(
                client_id=data['client_id'],
                api_key=data['api_key'],
            ).verify_connection()
        except OzonAPIError as exc:
            raise MarketplaceConnectionError(
                str(exc),
                code=exc.code,
                retry_after_seconds=exc.retry_after_seconds,
            ) from exc

    @staticmethod
    def _require_canary_admission(tenant, client_id: object) -> None:
        if not ozon_connection_enabled_for_account(tenant, client_id):
            raise MarketplaceProviderDisabled(
                'Подключение Ozon не разрешено для этого кабинета '
                'на текущем этапе rollout.',
            )

    @staticmethod
    def _require_read_only_confirmation(data: dict) -> None:
        if data.get('confirm_ozon_read_only_access') is not True:
            raise InvalidMarketplaceCredentials(
                'Подтвердите read-only проверку ролей, продавца и складов Ozon.',
            )

    @staticmethod
    def _profile_defaults(snapshot) -> dict:
        selected = snapshot.warehouses[0] if len(snapshot.warehouses) == 1 else None
        connection_status = OzonAccountProfile.ConnectionStatus.CONNECTED
        if not snapshot.warehouses:
            connection_status = OzonAccountProfile.ConnectionStatus.WAREHOUSE_MISSING
        elif len(snapshot.warehouses) > 1:
            connection_status = (
                OzonAccountProfile.ConnectionStatus.WAREHOUSE_SELECTION_REQUIRED
            )
        return {
            'connection_status': connection_status,
            'company_name': snapshot.company_name[:300],
            'seller_name': snapshot.seller_name[:300],
            'currency': snapshot.currency[:10],
            'roles': list(snapshot.roles),
            'api_methods': list(snapshot.api_methods),
            'api_key_expires_at': snapshot.api_key_expires_at,
            'warehouse_count': len(snapshot.warehouses),
            'selected_warehouse_id': selected.warehouse_id if selected else '',
            'selected_warehouse_name': selected.name if selected else '',
            'last_checked_at': timezone.now(),
        }

    @classmethod
    def create(cls, tenant, data: dict) -> MarketplaceAccount:
        client_id = str(data['client_id']).strip()
        cls._require_read_only_confirmation(data)
        cls._require_canary_admission(tenant, client_id)
        existing_account = MarketplaceAccount.objects.filter(
            tenant=tenant,
            marketplace=MarketplaceAccount.MARKETPLACE_OZON,
            external_id=client_id,
        ).first()
        if existing_account is not None:
            raise AccountAlreadyExists(
                'Этот аккаунт Ozon уже подключён к тенанту.',
                account_id=existing_account.pk,
            )
        snapshot = cls._verify_connection(data)
        credentials_enc = encrypt({
            'client_id': client_id,
            'api_key': data['api_key'],
        })
        profile_defaults = cls._profile_defaults(snapshot)
        try:
            with transaction.atomic():
                account = (
                    MarketplaceAccount.all_objects.select_for_update()
                    .filter(
                        marketplace=MarketplaceAccount.MARKETPLACE_OZON,
                        external_id=client_id,
                    )
                    .first()
                )
                if account is not None and account.tenant_id != tenant.pk:
                    raise AccountAlreadyExists(
                        'Этот кабинет Ozon уже управляется другим тенантом.',
                    )
                if account is not None and account.deleted_at is None:
                    raise AccountAlreadyExists(
                        'Этот аккаунт Ozon уже подключён к тенанту.',
                        account_id=account.pk,
                    )
                if account is None:
                    account = MarketplaceAccount.objects.create(
                        tenant=tenant,
                        name=data['name'],
                        marketplace=MarketplaceAccount.MARKETPLACE_OZON,
                        external_id=client_id,
                        credentials_enc=credentials_enc,
                    )
                else:
                    account.name = data['name']
                    account.credentials_enc = credentials_enc
                    account.is_active = True
                    account.deleted_at = None
                    account.save(update_fields=(
                        'name', 'credentials_enc', 'is_active', 'deleted_at',
                        'updated_at',
                    ))
                OzonAccountProfile.objects.update_or_create(
                    account=account,
                    defaults=profile_defaults,
                )
        except IntegrityError:
            owner = (
                MarketplaceAccount.all_objects.filter(
                    marketplace=MarketplaceAccount.MARKETPLACE_OZON,
                    external_id=client_id,
                ).first()
            )
            raise AccountAlreadyExists(
                'Этот аккаунт Ozon уже подключён к тенанту.',
                account_id=(
                    owner.pk
                    if owner is not None and owner.tenant_id == tenant.pk
                    else None
                ),
            )
        return account

    @classmethod
    def update_credentials(cls, account, data: dict) -> MarketplaceAccount:
        client_id = str(data['client_id']).strip()
        cls._require_read_only_confirmation(data)
        if client_id != account.external_id:
            raise InvalidMarketplaceCredentials(
                'Client-Id не совпадает с подключённым аккаунтом Ozon. '
                'Для другого кабинета создайте отдельный аккаунт.',
            )
        cls._require_canary_admission(account.tenant, client_id)
        snapshot = cls._verify_connection(data)
        credentials_enc = encrypt({
            'client_id': client_id,
            'api_key': data['api_key'],
        })
        with transaction.atomic():
            account = (
                type(account).all_objects.select_for_update()
                .get(pk=account.pk)
            )
            if client_id != account.external_id:
                raise InvalidMarketplaceCredentials(
                    'Идентификатор аккаунта Ozon изменился во время обновления.',
                )
            account.name = data['name']
            account.credentials_enc = credentials_enc
            account.save(update_fields=('name', 'credentials_enc', 'updated_at'))
            profile, _ = OzonAccountProfile.objects.update_or_create(
                account=account,
                defaults={
                    **cls._profile_defaults(snapshot),
                    # Every credential rotation closes mutations until the
                    # tenant explicitly reviews the new key's provider roles.
                    'product_write_enabled': False,
                    'commerce_auto_sync_enabled': False,
                },
            )
            account.ozon_profile = profile
        return account
