import datetime
import hmac
from decimal import Decimal, InvalidOperation
from functools import partial
from typing import Any, TypedDict, cast

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.marketplaces.listing_lifecycle import (
    clear_remote_observation,
    release_status_check,
)
from apps.marketplaces.listing_delivery import (
    durable_feed_run_enabled,
    listing_delivery_presentation,
    listing_publication_available,
)
from apps.marketplaces.feed_cutover import (
    private_feed_cutover_enabled,
    private_feed_fleet_enabled,
)
from apps.marketplaces.models import (
    AvitoAccountStatus,
    CategoryMapping,
    Listing,
    ListingStats,
    MarketplaceAccount,
    MarketplaceFeedRun,
    MarketplacePlacementAddress,
)
from apps.marketplaces.price_utils import (
    compute_price,
    effective_category_margin,
    effective_margin,
)


_LOCAL_STATUS_RECHECK_DELAY = datetime.timedelta(minutes=10)
_FEED_PROJECTION_STATUSES = frozenset({
    Listing.STATUS_ACTIVE,
    Listing.STATUS_PENDING,
})


class _StaleLocalListingIntent(RuntimeError):
    """Abort an intent and its already-written feed revision atomically."""


class _ListingExpectedState(TypedDict):
    expected_status: str
    expected_account_id: int
    expected_external_id: str | None
    expected_deleted_at: datetime.datetime | None


def _status_lifecycle_dual_write_enabled() -> bool:
    return settings.AVITO_STATUS_LIFECYCLE_MODE == 'dual_write'


def _feed_ingress_dual_write_enabled() -> bool:
    """Return whether local desired state must advance the DB ingress."""

    return settings.MARKETPLACE_FEED_INGRESS_MODE in {'dual_write', 'durable'}


def _durable_feed_run_enabled(account_id: int | None = None) -> bool:
    """Return whether the durable feed owner may receive new work."""

    fleet_enabled = (
        settings.MARKETPLACE_FEED_RUN_MODE == 'durable'
        and _status_lifecycle_dual_write_enabled()
    )
    return fleet_enabled or (
        account_id is not None and private_feed_cutover_enabled(account_id)
    )


def _listing_is_in_feed_projection(*, status: str, deleted_at) -> bool:
    return deleted_at is None and status in _FEED_PROJECTION_STATUSES


def _lock_feed_intent_accounts_and_endpoints(
    account_ids,
    *,
    tenant_id: int | None = None,
):
    """Lock a sorted account set and its optional stable endpoints."""

    from apps.marketplaces.models import MarketplaceFeedEndpoint

    normalized_ids = sorted({int(account_id) for account_id in account_ids})
    accounts = MarketplaceAccount.all_objects.select_for_update().filter(
        pk__in=normalized_ids,
    )
    if tenant_id is not None:
        accounts = accounts.filter(tenant_id=tenant_id)
    locked_accounts = list(accounts.order_by('pk'))
    if [account.pk for account in locked_accounts] != normalized_ids:
        raise MarketplaceAccount.DoesNotExist(
            'Marketplace account is missing or belongs to another tenant.',
        )
    list(
        MarketplaceFeedEndpoint.objects.select_for_update()
        .filter(account_id__in=normalized_ids)
        .order_by('account_id')
    )
    return locked_accounts


def _bump_locked_accounts_with_live_projection(account_ids) -> bool:
    """Advance each pre-locked account once if it owns projected rows."""

    normalized_ids = sorted({int(account_id) for account_id in account_ids})
    live_account_ids = set(
        Listing.objects.filter(
            account_id__in=normalized_ids,
            status__in=_FEED_PROJECTION_STATUSES,
        ).values_list('account_id', flat=True)
    )
    if not live_account_ids:
        return False
    from apps.marketplaces.feed_intents import bump_feed_intents

    bump_feed_intents(live_account_ids, timezone.now())
    return True


def _lock_tenant_avito_feed_accounts(tenant_id: int):
    """Lock all nondeleted Avito owners before a tenant-wide XML dependency."""

    from apps.marketplaces.models import MarketplaceFeedEndpoint

    accounts = list(
        MarketplaceAccount.all_objects.select_for_update()
        .filter(
            tenant_id=tenant_id,
            marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
            deleted_at__isnull=True,
        )
        .order_by('pk')
    )
    account_ids = [account.pk for account in accounts]
    list(
        MarketplaceFeedEndpoint.objects.select_for_update()
        .filter(account_id__in=account_ids)
        .order_by('account_id')
    )
    return accounts


def _merged_update_fields(*field_groups) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        field
        for fields in field_groups
        for field in fields
    ))


def _listing_expected_state(listing: Listing) -> _ListingExpectedState:
    return {
        'expected_status': listing.status,
        'expected_account_id': listing.account_id,
        'expected_external_id': listing.external_id,
        'expected_deleted_at': listing.deleted_at,
    }


def _local_status_due_at(listing: Listing):
    if listing.status == Listing.STATUS_ACTIVE or (
        listing.status == Listing.STATUS_ARCHIVING and bool(listing.external_id)
    ):
        return timezone.now() + _LOCAL_STATUS_RECHECK_DELAY
    return None


def _make_provider_status_check_due_now(listing: Listing) -> Listing:
    """Nudge one exact live provider identity without revoking an owner lease."""

    due_at = timezone.now()
    with transaction.atomic():
        account = (
            MarketplaceAccount.objects.select_for_update(of=('self',))
            .filter(
                pk=listing.account_id,
                tenant_id=listing.tenant_id,
                deleted_at__isnull=True,
                is_active=True,
            )
            .first()
        )
        if account is None:
            raise InvalidListingStatus('Аккаунт Avito недоступен для проверки.')
        current = (
            Listing.objects.select_for_update(of=('self',))
            .select_related('product', 'account', 'feed_run')
            .filter(
                pk=listing.pk,
                tenant_id=listing.tenant_id,
                account_id=account.pk,
                status__in={
                    Listing.STATUS_ACTIVE,
                    Listing.STATUS_REJECTED,
                    Listing.STATUS_ARCHIVING,
                },
            )
            .exclude(external_id__isnull=True)
            .exclude(external_id='')
            .first()
        )
        if current is None:
            raise InvalidListingStatus(
                'У объявления нет действующего Avito ID для проверки.',
            )
        current.next_status_check_at = due_at
        current.save(update_fields=['next_status_check_at'])
        _min_nudge_account_status_due(account.pk, due_at)
        return current


def _min_nudge_account_status_due(account_id: int, due_at) -> int:
    if due_at is None:
        return 0
    return MarketplaceAccount.objects.filter(
        pk=account_id,
        is_active=True,
    ).filter(
        Q(status_batch_due_at__isnull=True)
        | Q(status_batch_due_at__gt=due_at),
    ).update(status_batch_due_at=due_at)


def _copy_listing_row(target: Listing, source: Listing) -> None:
    for model_field in Listing._meta.concrete_fields:
        setattr(target, model_field.attname, getattr(source, model_field.attname))
    target._state.fields_cache.clear()


def _save_local_listing_intent(
    listing: Listing,
    update_fields,
    *,
    expected_status: str,
    expected_account_id: int,
    expected_external_id: str | None,
    expected_deleted_at,
    expected_product_updated_at=None,
    reset_provider_identity: bool = False,
    require_target_account_active: bool = False,
    block_provider_owned_pending: bool = False,
) -> bool:
    """Compare-and-apply one local intent under account-to-listing locks.

    The caller may have read the row before a provider/local worker committed
    a newer transition.  We therefore copy only the requested business fields
    after revalidating the complete row generation. Lifecycle values are
    recomputed from that locked post-intent row rather than copied from the
    caller's stale instance.
    """

    fields = tuple(dict.fromkeys(update_fields))
    intended_values = {}
    for field_name in fields:
        model_field = cast(Any, Listing._meta.get_field(field_name))
        intended_values[model_field.attname] = getattr(
            listing, model_field.attname,
        )

    if reset_provider_identity:
        listing.external_id = None
        listing.external_url = ''
        listing.feed_run_id = None
        intended_values['external_id'] = None
        intended_values['external_url'] = ''
        intended_values['feed_run_id'] = None
        fields = _merged_update_fields(
            fields,
            ('external_id', 'external_url', 'feed_run'),
        )

    lifecycle_enabled = _status_lifecycle_dual_write_enabled()
    feed_ingress_enabled = _feed_ingress_dual_write_enabled()
    if (
        not lifecycle_enabled
        and not feed_ingress_enabled
        and not block_provider_owned_pending
    ):
        listing.save(update_fields=fields)
        return True

    from apps.marketplaces.feed_intents import bump_feed_intents
    from apps.marketplaces.models import (
        MarketplaceAccount,
        MarketplaceFeedEndpoint,
    )

    intended_account_id = intended_values.get(
        'account_id', expected_account_id,
    )
    account_ids = {
        expected_account_id,
        intended_account_id,
    }

    def _product_generation_matches(current: Listing) -> bool:
        if expected_product_updated_at is None:
            return True
        from apps.products.models import Product

        return Product.objects.filter(
            pk=current.product_id,
            updated_at=expected_product_updated_at,
            deleted_at__isnull=True,
        ).exists()

    def _matches_expected(current: Listing) -> bool:
        return (
            current.status == expected_status
            and current.account_id == expected_account_id
            and current.external_id == expected_external_id
            and current.deleted_at == expected_deleted_at
            and _product_generation_matches(current)
        )

    try:
        with transaction.atomic():
            locked_accounts = list(
                MarketplaceAccount.all_objects.select_for_update()
                .filter(pk__in=account_ids)
                .order_by('pk')
            )
            locked_accounts_by_id = {
                account.pk: account for account in locked_accounts
            }
            if feed_ingress_enabled:
                # The feed primitive re-selects these rows to validate revision
                # parity.  Lock them before reading/locking Listing so that its
                # internal account->endpoint order acquires no late lock.
                list(
                    MarketplaceFeedEndpoint.objects.select_for_update()
                    .filter(account_id__in=account_ids)
                    .order_by('account_id')
                )

            # Account ownership is the writer fence for Listing.  Read and
            # validate before taking the row lock so the transactional feed
            # revision can advance in strict account->endpoint->listing order.
            current_snapshot = (
                Listing.all_objects.filter(pk=listing.pk).first()
            )
            if current_snapshot is None:
                return False
            if (
                block_provider_owned_pending
                and current_snapshot.status == Listing.STATUS_PENDING
            ):
                current_run = None
                if current_snapshot.feed_run_id is not None:
                    current_run = (
                        MarketplaceFeedRun.objects.select_for_update()
                        .filter(pk=current_snapshot.feed_run_id)
                        .first()
                    )
                delivery = listing_delivery_presentation(
                    current_snapshot,
                    run=current_run,
                    durable_enabled=durable_feed_run_enabled(
                        current_snapshot.account_id,
                    ),
                )
                if delivery.lifecycle_actions_blocked:
                    raise InvalidListingStatus(
                        'Нельзя архивировать, удалять или переносить объявление '
                        'на другой Avito-аккаунт, пока неизвестно, принял ли '
                        'Avito предыдущую отправку. Дождитесь автоматической '
                        'сверки или выполните ручную сверку запуска.',
                    )
            target_account = locked_accounts_by_id.get(intended_account_id)
            target_account_is_writable = (
                target_account is not None
                and target_account.deleted_at is None
                and target_account.tenant_id == current_snapshot.tenant_id
                and (
                    target_account.is_active
                    or not require_target_account_active
                )
            )
            if (
                not _matches_expected(current_snapshot)
                or not target_account_is_writable
            ):
                _copy_listing_row(listing, current_snapshot)
                return False

            before_values = {
                field_name: getattr(current_snapshot, field_name)
                for field_name in intended_values
            }
            before_live = _listing_is_in_feed_projection(
                status=current_snapshot.status,
                deleted_at=current_snapshot.deleted_at,
            )
            for field_name, value in intended_values.items():
                setattr(current_snapshot, field_name, value)
            after_live = _listing_is_in_feed_projection(
                status=current_snapshot.status,
                deleted_at=current_snapshot.deleted_at,
            )
            projection_changed = any(
                before_values[field_name] != value
                for field_name, value in intended_values.items()
            )
            if feed_ingress_enabled and projection_changed:
                projection_account_ids = set()
                if before_live:
                    projection_account_ids.add(expected_account_id)
                if after_live:
                    projection_account_ids.add(current_snapshot.account_id)
                if projection_account_ids:
                    bump_feed_intents(
                        projection_account_ids,
                        timezone.now(),
                    )

            if feed_ingress_enabled and after_live:
                # Product writers use the same account->endpoint->product
                # order.  Hold the source row before Listing can enter the
                # live projection so a concurrent content/delete generation
                # cannot fall between this bump and the membership commit.
                from apps.products.models import Product

                locked_product = (
                    Product.all_objects.select_for_update()
                    .filter(pk=current_snapshot.product_id)
                    .only('pk', 'updated_at', 'deleted_at')
                    .first()
                )
                product_is_current = (
                    locked_product is not None
                    and locked_product.deleted_at is None
                    and (
                        expected_product_updated_at is None
                        or locked_product.updated_at
                        == expected_product_updated_at
                    )
                )
                if not product_is_current:
                    raise _StaleLocalListingIntent

            current = (
                Listing.all_objects.select_for_update()
                .filter(pk=listing.pk)
                .first()
            )
            if current is None or not _matches_expected(current):
                if current is not None:
                    _copy_listing_row(listing, current)
                raise _StaleLocalListingIntent
            for field_name, value in intended_values.items():
                setattr(current, field_name, value)

            lifecycle_fields: tuple[str, ...] = ()
            if lifecycle_enabled and reset_provider_identity:
                observation_fields = clear_remote_observation().apply_to(current)
                claim_fields = release_status_check(
                    next_status_check_at=None,
                ).apply_to(current)
                lifecycle_fields = _merged_update_fields(
                    observation_fields, claim_fields,
                )
            elif lifecycle_enabled:
                lifecycle_fields = release_status_check(
                    next_status_check_at=_local_status_due_at(current),
                ).apply_to(current)

            saved_fields = _merged_update_fields(fields, lifecycle_fields)
            current.save(update_fields=saved_fields)
            for field_name in saved_fields:
                model_field = cast(Any, Listing._meta.get_field(field_name))
                setattr(
                    listing,
                    model_field.attname,
                    getattr(current, model_field.attname),
                )
            listing._state.fields_cache.clear()

            due_at = current.next_status_check_at if lifecycle_enabled else None
            if due_at is not None:
                if current.account_id not in locked_accounts_by_id:
                    raise RuntimeError(
                        'Listing due cursor requires its current account lock.',
                    )
                _min_nudge_account_status_due(current.account_id, due_at)
            return True
    except _StaleLocalListingIntent:
        return False


def _provider_identity_reset_kwargs() -> dict[str, object]:
    """Build the bulk reset used when an account changes provider identity."""

    values: dict[str, object] = {
        'external_id': None,
        'external_url': '',
        'feed_run_id': None,
    }
    if _status_lifecycle_dual_write_enabled():
        values.update(clear_remote_observation().as_update_kwargs())
        values.update(release_status_check(next_status_check_at=None).as_update_kwargs())
    return values


def _bump_account_feed_projection_if_live(account_id: int) -> bool:
    """Advance a pre-locked account when its identity/defaults affect XML."""

    if not _feed_ingress_dual_write_enabled():
        return False
    if not Listing.objects.filter(
        account_id=account_id,
        status__in=_FEED_PROJECTION_STATUSES,
    ).exists():
        return False
    from apps.marketplaces.feed_intents import bump_feed_intents

    bump_feed_intents([account_id], timezone.now())
    return True


def _reset_account_status_batch(account) -> tuple[str, ...]:
    """Drop scheduler state that belongs to a previous provider identity."""

    if not _status_lifecycle_dual_write_enabled():
        return ()
    account.status_batch_due_at = None
    account.status_batch_cooldown_until = None
    account.status_batch_claim_token = None
    account.status_batch_claimed_until = None
    return (
        'status_batch_due_at',
        'status_batch_cooldown_until',
        'status_batch_claim_token',
        'status_batch_claimed_until',
    )


class CategoryMappingService:
    """Сервис маппинга категорий из источников данных в категории Avito."""

    @staticmethod
    def get_or_suggest(tenant, category_1c: str) -> CategoryMapping | None:
        """Возвращает маппинг для категории или None если не найден."""
        return CategoryMapping.objects.filter(
            tenant=tenant,
            marketplace=CategoryMapping.MARKETPLACE_AVITO,
            category_source=category_1c,
        ).first()

    @staticmethod
    def bulk_create_from_dict(tenant, mappings: dict) -> list[CategoryMapping]:
        """
        Создаёт или обновляет маппинги из словаря {source: {target, category_id, ...}}.

        Идемпотентен — повторный вызов с теми же данными не создаёт дублей.
        """
        if not _feed_ingress_dual_write_enabled():
            result = []
            for source, data in mappings.items():
                obj, _ = CategoryMapping.objects.update_or_create(
                    tenant=tenant,
                    marketplace=CategoryMapping.MARKETPLACE_AVITO,
                    category_source=source,
                    defaults={
                        'category_target': data['category_target'],
                        'category_id': data['category_id'],
                        'attributes_map': data.get('attributes_map', {}),
                    },
                )
                result.append(obj)
            return result

        sources = sorted(mappings)
        with transaction.atomic():
            locked_accounts = _lock_tenant_avito_feed_accounts(tenant.pk)
            existing_by_source = {
                mapping.category_source: mapping
                for mapping in CategoryMapping.objects.filter(
                    tenant=tenant,
                    marketplace=CategoryMapping.MARKETPLACE_AVITO,
                    category_source__in=sources,
                )
            }
            changed = False
            for source, data in mappings.items():
                current = existing_by_source.get(source)
                desired = (
                    data['category_target'],
                    data['category_id'],
                    data.get('attributes_map', {}),
                )
                if current is None or desired != (
                    current.category_target,
                    current.category_id,
                    current.attributes_map,
                ):
                    changed = True
                    break
            if changed:
                _bump_locked_accounts_with_live_projection(
                    [account.pk for account in locked_accounts],
                )
            # Mapping locks are deliberately below account/endpoint/bump.
            list(
                CategoryMapping.objects.select_for_update()
                .filter(
                    tenant=tenant,
                    marketplace=CategoryMapping.MARKETPLACE_AVITO,
                    category_source__in=sources,
                )
                .order_by('pk')
            )
            result = []
            for source, data in mappings.items():
                obj, _ = CategoryMapping.objects.update_or_create(
                    tenant=tenant,
                    marketplace=CategoryMapping.MARKETPLACE_AVITO,
                    category_source=source,
                    defaults={
                        'category_target': data['category_target'],
                        'category_id': data['category_id'],
                        'attributes_map': data.get('attributes_map', {}),
                    },
                )
                result.append(obj)
            return result

    @staticmethod
    def upsert(tenant, data: dict) -> CategoryMapping:
        """Create/update one mapping with one tenant-wide feed successor."""

        result = CategoryMappingService.bulk_create_from_dict(tenant, {
            data['category_source']: {
                'category_target': data['category_target'],
                'category_id': data['category_id'],
                'attributes_map': data.get('attributes_map', {}),
            },
        })
        return result[0]

    @staticmethod
    def update(mapping: CategoryMapping, data: dict) -> CategoryMapping:
        """Replace a mapping and advance its version transactionally."""

        if not _feed_ingress_dual_write_enabled():
            for field, value in data.items():
                setattr(mapping, field, value)
            mapping.version += 1
            mapping.save()
            return mapping

        with transaction.atomic():
            locked_accounts = _lock_tenant_avito_feed_accounts(
                mapping.tenant_id,
            )
            snapshot = CategoryMapping.objects.filter(pk=mapping.pk).first()
            if snapshot is None:
                raise CategoryMapping.DoesNotExist
            desired_changed = any(
                getattr(snapshot, field) != value
                for field, value in data.items()
            )
            if desired_changed:
                _bump_locked_accounts_with_live_projection(
                    [account.pk for account in locked_accounts],
                )
            current = CategoryMapping.objects.select_for_update().get(
                pk=mapping.pk,
                tenant_id=mapping.tenant_id,
            )
            for field, value in data.items():
                setattr(current, field, value)
            current.version += 1
            current.save()
            return current

    @staticmethod
    def delete(tenant, mapping_id: int) -> int:
        """Delete one mapping behind a tenant-wide feed revision."""

        if not _feed_ingress_dual_write_enabled():
            deleted, _ = CategoryMapping.objects.filter(
                pk=mapping_id,
                tenant=tenant,
            ).delete()
            return deleted

        with transaction.atomic():
            locked_accounts = _lock_tenant_avito_feed_accounts(tenant.pk)
            exists = CategoryMapping.objects.filter(
                pk=mapping_id,
                tenant=tenant,
            ).exists()
            if not exists:
                return 0
            _bump_locked_accounts_with_live_projection(
                [account.pk for account in locked_accounts],
            )
            mapping = CategoryMapping.objects.select_for_update().get(
                pk=mapping_id,
                tenant=tenant,
            )
            deleted, _ = mapping.delete()
            return deleted

    @staticmethod
    def get_unmapped_categories(tenant) -> list[str]:
        """Возвращает категории из товаров тенанта, для которых ещё нет маппинга."""
        from apps.products.models import Product
        mapped = set(
            CategoryMapping.objects.filter(tenant=tenant, marketplace=CategoryMapping.MARKETPLACE_AVITO)
            .values_list('category_source', flat=True)
        )
        all_categories = set(
            Product.objects.filter(tenant=tenant)
            .exclude(category_1c='')
            .values_list('category_1c', flat=True)
            .distinct()
        )
        return sorted(all_categories - mapped)


class MarketplacePlacementAddressService:
    """Feed-safe CRUD for account placement/contact defaults."""

    @staticmethod
    def create(tenant, data: dict) -> MarketplacePlacementAddress:
        account = data['account']
        values = {key: value for key, value in data.items() if key != 'account'}
        if not _feed_ingress_dual_write_enabled():
            if values.get('is_default'):
                MarketplacePlacementAddress.objects.filter(
                    tenant=tenant,
                    account=account,
                    is_default=True,
                ).update(is_default=False)
            return MarketplacePlacementAddress.objects.create(
                tenant=tenant,
                account=account,
                **values,
            )

        with transaction.atomic():
            _lock_feed_intent_accounts_and_endpoints(
                [account.pk],
                tenant_id=tenant.pk,
            )
            # Creating an active/default address can change inherited XML.
            _bump_locked_accounts_with_live_projection([account.pk])
            list(
                MarketplacePlacementAddress.objects.select_for_update()
                .filter(tenant=tenant, account_id=account.pk)
                .order_by('pk')
            )
            if values.get('is_default'):
                MarketplacePlacementAddress.objects.filter(
                    tenant=tenant,
                    account_id=account.pk,
                    is_default=True,
                ).update(is_default=False)
            return MarketplacePlacementAddress.objects.create(
                tenant=tenant,
                account_id=account.pk,
                **values,
            )

    @staticmethod
    def update(
        address: MarketplacePlacementAddress,
        tenant,
        data: dict,
    ) -> MarketplacePlacementAddress:
        if not _feed_ingress_dual_write_enabled():
            if data.get('is_default'):
                account = data.get('account', address.account)
                MarketplacePlacementAddress.objects.filter(
                    tenant=tenant,
                    account=account,
                    is_default=True,
                ).exclude(pk=address.pk).update(is_default=False)
            for field, value in data.items():
                setattr(address, field, value)
            address.save()
            return address

        new_account = data.get('account', address.account)
        account_ids = {address.account_id, new_account.pk}
        with transaction.atomic():
            _lock_feed_intent_accounts_and_endpoints(
                account_ids,
                tenant_id=tenant.pk,
            )
            snapshot = MarketplacePlacementAddress.objects.filter(
                pk=address.pk,
                tenant=tenant,
            ).first()
            if snapshot is None:
                raise MarketplacePlacementAddress.DoesNotExist
            if snapshot.account_id != address.account_id:
                # The caller was authorized against an older owner. Retrying
                # under a newly fetched row is required so the lock set starts
                # with the actual old account rather than acquiring it late.
                raise MarketplacePlacementAddress.DoesNotExist
            resulting_is_default = data.get(
                'is_default',
                snapshot.is_default,
            )
            resulting_account = data.get('account', snapshot.account)
            resulting_account_id = getattr(
                resulting_account,
                'pk',
                resulting_account,
            )
            changed = False
            for field, value in data.items():
                model_field = cast(
                    Any,
                    MarketplacePlacementAddress._meta.get_field(field),
                )
                current_value = getattr(snapshot, model_field.attname)
                intended_value = (
                    getattr(value, 'pk', value)
                    if model_field.is_relation
                    else value
                )
                if current_value != intended_value:
                    changed = True
                    break
            # Legacy races can leave multiple defaults because the schema has
            # no partial uniqueness constraint. Reselecting an already-default
            # row still demotes its peer below and changes inherited XML.
            if resulting_is_default and MarketplacePlacementAddress.objects.filter(
                tenant=tenant,
                account_id=resulting_account_id,
                is_default=True,
            ).exclude(pk=snapshot.pk).exists():
                changed = True
            if changed:
                _bump_locked_accounts_with_live_projection(account_ids)
            # Lock every default candidate only after account/endpoint/bump.
            list(
                MarketplacePlacementAddress.objects.select_for_update()
                .filter(tenant=tenant, account_id__in=account_ids)
                .order_by('pk')
            )
            current = MarketplacePlacementAddress.objects.get(
                pk=address.pk,
                tenant=tenant,
            )
            if resulting_is_default:
                MarketplacePlacementAddress.objects.filter(
                    tenant=tenant,
                    account=resulting_account,
                    is_default=True,
                ).exclude(pk=current.pk).update(is_default=False)
            for field, value in data.items():
                setattr(current, field, value)
            current.save()
            return current

    @staticmethod
    def deactivate(
        address: MarketplacePlacementAddress,
        tenant,
    ) -> MarketplacePlacementAddress:
        if not _feed_ingress_dual_write_enabled():
            address.is_active = False
            address.save(update_fields=['is_active'])
            return address

        with transaction.atomic():
            _lock_feed_intent_accounts_and_endpoints(
                [address.account_id],
                tenant_id=tenant.pk,
            )
            snapshot = MarketplacePlacementAddress.objects.filter(
                pk=address.pk,
                tenant=tenant,
            ).first()
            if snapshot is None:
                raise MarketplacePlacementAddress.DoesNotExist
            if snapshot.account_id != address.account_id:
                raise MarketplacePlacementAddress.DoesNotExist
            if snapshot.is_active:
                _bump_locked_accounts_with_live_projection(
                    [snapshot.account_id],
                )
            current = MarketplacePlacementAddress.objects.select_for_update().get(
                pk=address.pk,
                tenant=tenant,
            )
            current.is_active = False
            current.save(update_fields=['is_active'])
            return current


class ListingNotFound(Exception):
    """Листинг тенанта не найден."""


class InvalidListingStatus(Exception):
    """Операция недопустима для текущего статуса листинга."""


class ListingPublicationValidationError(InvalidListingStatus):
    """A new provider submission is blocked by editable listing fields."""

    def __init__(self, field_errors: dict[str, list[str]]):
        super().__init__('Исправьте отмеченные поля перед отправкой в Avito.')
        self.field_errors = field_errors


class ListingAccountConflict(Exception):
    """Для товара уже есть листинг на выбранном аккаунте."""


class ListingBulkLimitExceeded(ValueError):
    """A direct bulk listing operation selected more rows than the API cap."""


class NoActiveAccounts(Exception):
    """У тенанта нет ни одного активного аккаунта маркетплейса."""


class AccountAlreadyExists(Exception):
    """Аккаунт с таким external_id уже существует у тенанта."""

    def __init__(self, message: str, *, account_id: int | None = None):
        super().__init__(message)
        self.account_id = account_id


class InvalidMarketplaceCredentials(Exception):
    """Credentials маркетплейса не прошли проверку через API."""


class MarketplaceAccountFeedConflict(Exception):
    """Изменение аккаунта конфликтует с активным feed-процессом."""


def _assert_legacy_feed_cursor_mutation_safe(account) -> None:
    """Block account drift while a legacy provider POST is unresolved."""

    if not (
        settings.MARKETPLACE_FEED_RUN_MODE == 'legacy'
        and settings.MARKETPLACE_FEED_INGRESS_MODE
        in {'legacy', 'dual_write', 'durable'}
    ):
        return
    if (
        account.feed_intent_revision
        > account.feed_intent_dispatched_revision
        and account.feed_intent_due_at is None
    ):
        raise MarketplaceAccountFeedConflict(
            'Нельзя изменить аккаунт: результат предыдущей отправки фида '
            'ещё не подтверждён. Сначала выполните ручную сверку cursor hold.',
        )


def _assert_account_identity_mutation_safe(account) -> None:
    """Не даёт сменить credentials поверх уже отправленного durable run."""

    _assert_legacy_feed_cursor_mutation_safe(account)

    from apps.marketplaces.feed_workflow import (
        FeedSubmissionOutcomeUncertain,
        assert_no_submitted_feed_owner,
    )

    try:
        assert_no_submitted_feed_owner(
            account.pk,
            reason='Marketplace account credentials or provider identity changed.',
        )
    except FeedSubmissionOutcomeUncertain:
        raise MarketplaceAccountFeedConflict(
            'Нельзя изменить подключение: результат предыдущей отправки '
            'фида ещё не подтверждён. Сначала выполните ручную сверку запуска.',
        ) from None


def _lock_marketplace_feed_endpoint(account_id: int):
    """Блокирует endpoint сразу после блокировки его аккаунта-владельца."""

    from apps.marketplaces.models import MarketplaceFeedEndpoint

    return (
        MarketplaceFeedEndpoint.objects.select_for_update()
        .filter(account_id=account_id)
        .first()
    )


def _assert_feed_endpoint_identity_mutation_safe(endpoint) -> None:
    """Запрещает смену identity после начала изменения профиля Avito."""

    if endpoint is None:
        return
    from apps.marketplaces.models import MarketplaceFeedEndpoint

    if endpoint.profile_state in {
        MarketplaceFeedEndpoint.ProfileState.MIGRATING,
        MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
        MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
        MarketplaceFeedEndpoint.ProfileState.VERIFIED,
    }:
        raise MarketplaceAccountFeedConflict(
            'Нельзя изменить подключение, пока stable feed endpoint '
            'используется или сверяется с профилем площадки.',
        )


def _assert_feed_endpoint_availability_mutation_safe(
    endpoint,
    *,
    destructive: bool = False,
) -> None:
    """Защищает доступность аккаунта около изменения профиля Avito."""

    if endpoint is None:
        return
    from apps.marketplaces.models import MarketplaceFeedEndpoint

    if endpoint.profile_state in {
        MarketplaceFeedEndpoint.ProfileState.MIGRATING,
        MarketplaceFeedEndpoint.ProfileState.UPDATE_UNKNOWN,
    } or (
        destructive
        and endpoint.profile_state in {
            MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
            MarketplaceFeedEndpoint.ProfileState.VERIFIED,
        }
    ):
        raise MarketplaceAccountFeedConflict(
            'Нельзя отключить аккаунт во время миграции feed-профиля. '
            'Сначала завершите или сверите миграцию.',
        )


def _fence_marketplace_feed_endpoint_identity(account, endpoint) -> None:
    """Отзывает уже заблокированную capability после смены credentials."""

    if endpoint is None:
        return
    from apps.marketplaces.feed_workflow import account_identity_digest
    from apps.marketplaces.models import MarketplaceFeedEndpoint

    max_revision = (1 << 63) - 1
    if (
        endpoint.capability_revision >= max_revision
        or endpoint.profile_revision >= max_revision
    ):
        raise OverflowError('Marketplace feed endpoint revision is exhausted.')

    endpoint.owner_identity_digest = account_identity_digest(account)
    endpoint.capability_revision += 1
    endpoint.previous_token_key_id = ''
    endpoint.serve_enabled = False
    endpoint.profile_state = MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW
    endpoint.profile_fingerprint = ''
    endpoint.profile_verified_at = None
    endpoint.profile_revision += 1
    endpoint.save(update_fields=(
        'owner_identity_digest',
        'capability_revision',
        'previous_token_key_id',
        'serve_enabled',
        'profile_state',
        'profile_fingerprint',
        'profile_verified_at',
        'profile_revision',
        'updated_at',
    ))


def _refresh_new_feed_endpoint_identity(account, endpoint) -> None:
    """Re-key an endpoint that has never been exposed or posted to Avito."""

    if endpoint is None:
        return
    from apps.marketplaces.feed_workflow import account_identity_digest
    from apps.marketplaces.models import MarketplaceFeedEndpoint

    if (
        endpoint.profile_state != MarketplaceFeedEndpoint.ProfileState.NEW
        or endpoint.serve_enabled
    ):
        raise MarketplaceAccountFeedConflict(
            'Нельзя изменить подключение после начала настройки профиля Avito.',
        )
    max_revision = (1 << 63) - 1
    if (
        endpoint.capability_revision >= max_revision
        or endpoint.profile_revision >= max_revision
    ):
        raise OverflowError('Marketplace feed endpoint revision is exhausted.')
    endpoint.owner_identity_digest = account_identity_digest(account)
    endpoint.capability_revision += 1
    endpoint.previous_token_key_id = ''
    endpoint.profile_fingerprint = ''
    endpoint.profile_verified_at = None
    endpoint.profile_revision += 1
    endpoint.save(update_fields=(
        'owner_identity_digest',
        'capability_revision',
        'previous_token_key_id',
        'profile_fingerprint',
        'profile_verified_at',
        'profile_revision',
        'updated_at',
    ))


def _invalidate_avito_access_token_after_commit(account) -> None:
    """Удаляет старый OAuth token только после успешного commit credentials."""

    from apps.marketplaces.adapters.avito.auth import AvitoAuthManager

    transaction.on_commit(partial(AvitoAuthManager().invalidate, account))


class BulkActionItem(TypedDict):
    id: int
    status: str
    message: str


class BulkActionResult(TypedDict):
    total: int
    success: int
    skipped: int
    errors: int
    items: list[BulkActionItem]


class ListingService:
    """Сервис управления объявлениями: создание, маршрутизация по типу изменения."""

    @staticmethod
    def get_for_tenant(listing_id: int, tenant) -> Listing:
        """Возвращает листинг тенанта или бросает ListingNotFound."""
        try:
            return (
                Listing.objects
                .select_related(
                    'product',
                    'product__catalog_category',
                    'account',
                    'feed_run',
                    'placement_address',
                    'bulk_placement_address',
                )
                .prefetch_related('product__images')
                .get(pk=listing_id, tenant=tenant)
            )
        except Listing.DoesNotExist:
            raise ListingNotFound(f'Листинг {listing_id} не найден')

    @staticmethod
    def approve(listing_id: int, tenant) -> Listing:
        """
        Одобряет листинг requires_review и ставит задачу публикации в Celery.

        Raises:
            ListingNotFound: листинг не принадлежит тенанту.
            InvalidListingStatus: листинг не в статусе requires_review.
        """
        listing = ListingService.get_for_tenant(listing_id, tenant)
        if listing.status != Listing.STATUS_REQUIRES_REVIEW:
            raise InvalidListingStatus(
                f'Одобрить можно только листинг в статусе requires_review, '
                f'текущий статус: {listing.status}'
            )
        from apps.marketplaces.adapters.avito.feed_builder import (
            avito_publication_field_errors,
        )
        field_errors = avito_publication_field_errors(listing)
        if field_errors:
            raise ListingPublicationValidationError(field_errors)
        expected = _listing_expected_state(listing)
        listing.status = Listing.STATUS_QUEUED
        listing.rejection_reason = ''
        applied = _save_local_listing_intent(
            listing,
            ('status', 'rejection_reason'),
            **expected,
        )

        if applied:
            lid = listing.pk
            transaction.on_commit(lambda: _enqueue_publish_or_update(lid, is_new=True))
        return listing

    @staticmethod
    def publish(listing_id: int, tenant) -> Listing:
        """
        Публикует черновик, отклонённый, архивный или упёршийся в лимит листинг на Avito.

        Raises:
            ListingNotFound: листинг не принадлежит тенанту.
            InvalidListingStatus: листинг не в подходящем статусе для публикации.
        """
        listing = ListingService.get_for_tenant(listing_id, tenant)
        if not listing_publication_available(listing):
            raise InvalidListingStatus(
                'Публикация доступна для черновика, отклонённого, архивного, '
                'достигшего лимита объявления или доказанной ошибки до отправки '
                f'в Avito; текущая стадия: {listing_delivery_presentation(listing).stage}'
            )
        from apps.marketplaces.adapters.avito.feed_builder import (
            avito_publication_field_errors,
        )
        field_errors = avito_publication_field_errors(listing)
        if field_errors:
            raise ListingPublicationValidationError(field_errors)
        retry_failed_delivery = (
            listing.status == Listing.STATUS_PENDING
            and listing_delivery_presentation(listing).stage == 'delivery_failed'
        )
        expected = _listing_expected_state(listing)
        listing.status = Listing.STATUS_QUEUED
        # Сбрасываем причину прошлого отклонения, чтобы старый текст не висел
        # на карточке, пока идёт новая публикация.
        listing.rejection_reason = ''
        update_fields = ['status', 'rejection_reason']
        if retry_failed_delivery:
            # Preserve the failed run as audit evidence, but detach the listing
            # so the next durable generation owns a fresh, truthful lifecycle.
            listing.feed_run = None
            update_fields.append('feed_run')
        applied = _save_local_listing_intent(
            listing,
            update_fields,
            **expected,
        )
        if applied:
            lid = listing.pk
            transaction.on_commit(lambda: _enqueue_publish_or_update(lid, is_new=True))
        return listing

    @staticmethod
    def archive(listing_id: int, tenant) -> Listing:
        """Снимает листинг с публикации через удаление из фида Avito."""
        listing = ListingService.get_for_tenant(listing_id, tenant)
        if listing.status in (Listing.STATUS_ARCHIVING, Listing.STATUS_ARCHIVED, Listing.STATUS_DELETED):
            raise InvalidListingStatus(f'Листинг уже в статусе {listing.status}')
        # Честный статус: «Снимается» — переключим в «В архиве» только после
        # подтверждения снятия от Avito (autoload пакетный, не мгновенный).
        expected = _listing_expected_state(listing)
        listing.status = Listing.STATUS_ARCHIVING
        applied = _save_local_listing_intent(
            listing,
            ('status',),
            block_provider_owned_pending=True,
            **expected,
        )
        if applied:
            lid = listing.pk
            transaction.on_commit(lambda: _enqueue_unpublish(lid))
        return listing

    @staticmethod
    def delete(listing_id: int, tenant) -> Listing:
        """Удаляет листинг локально и отправляет Remove в feed, если есть external_id."""
        listing = ListingService.get_for_tenant(listing_id, tenant)
        if listing.status == Listing.STATUS_DELETED:
            raise InvalidListingStatus('Листинг уже удалён')
        expected = _listing_expected_state(listing)
        listing.status = Listing.STATUS_DELETED
        applied = _save_local_listing_intent(
            listing,
            ('status',),
            block_provider_owned_pending=True,
            **expected,
        )
        if applied:
            lid = listing.pk
            transaction.on_commit(lambda: _enqueue_delete(lid))
        return listing

    @staticmethod
    def check_avito_status(listing_id: int, tenant) -> Listing:
        """Ставит ручную проверку доставки или живого статуса объявления."""
        listing = ListingService.get_for_tenant(listing_id, tenant)
        delivery = listing_delivery_presentation(listing)
        if not delivery.can_check_avito_status:
            raise InvalidListingStatus(
                f'Проверка Avito сейчас недоступна: {delivery.label.lower()}.',
            )
        if listing.status == Listing.STATUS_PENDING:
            account_id = listing.account_id
            transaction.on_commit(lambda: _enqueue_poll_feed_results(account_id))
            return listing

        if _status_lifecycle_dual_write_enabled():
            listing = _make_provider_status_check_due_now(listing)
        listing_id = listing.pk
        is_archiving = listing.status == Listing.STATUS_ARCHIVING
        transaction.on_commit(
            lambda: _enqueue_provider_listing_status_check(
                listing_id,
                is_archiving=is_archiving,
            ),
        )
        return listing

    @staticmethod
    def request_regenerate(
        listing_id: int,
        tenant,
        *,
        durable_deduplication_key: str | None = None,
    ) -> Listing:
        """
        Инициирует перегенерацию AI-описания.

        Доступно для статусов requires_review, draft, rejected.
        После генерации листинг автоматически публикуется (если confidence ≥ 0.5)
        или снова попадёт на проверку.

        Raises:
            ListingNotFound: листинг не принадлежит тенанту.
            InvalidListingStatus: листинг не в подходящем статусе.
        """
        listing = ListingService.get_for_tenant(listing_id, tenant)
        regenerable = (
            Listing.STATUS_REQUIRES_REVIEW,
            Listing.STATUS_DRAFT,
            Listing.STATUS_REJECTED,
        )
        if listing.status not in regenerable:
            raise InvalidListingStatus(
                f'Перегенерация недоступна для статуса {listing.status}'
            )
        product_id = listing.product_id
        if durable_deduplication_key:
            from apps.products.models import Product
            from apps.products.services import ProductService

            product = Product.objects.select_related('tenant').get(pk=product_id)
            submission = ProductService.schedule_ai_generation(
                product,
                tenant,
                deduplication_key=durable_deduplication_key,
            )
            # The API transaction persists this exact durable result on its
            # paid-ingress intent.  Keeping it transient avoids changing the
            # public Listing model solely for response plumbing.
            listing.__dict__['_regeneration_submission'] = submission
        else:
            transaction.on_commit(lambda: _enqueue_ai_generation(product_id))
        return listing

    @staticmethod
    def update_content(listing_id: int, tenant, title: str | None, description_ai: str | None) -> Listing:
        """
        Обновляет заголовок и/или AI-описание листинга вручную.

        Не меняет статус — оператор редактирует текст перед одобрением.

        Raises:
            ListingNotFound: листинг не принадлежит тенанту.
            InvalidListingStatus: листинг нельзя редактировать (active или deleted).
        """
        listing = ListingService.get_for_tenant(listing_id, tenant)
        if listing.status in (Listing.STATUS_ACTIVE, Listing.STATUS_DELETED):
            raise InvalidListingStatus(
                f'Нельзя редактировать листинг в статусе {listing.status}'
            )
        expected = _listing_expected_state(listing)
        update_fields = []
        if title is not None:
            listing.title = title[:300]
            update_fields.append('title')
        if description_ai is not None:
            listing.description_ai = description_ai
            update_fields.append('description_ai')
        if update_fields:
            _save_local_listing_intent(
                listing,
                update_fields,
                **expected,
            )
        return listing

    @staticmethod
    def update_listing_fields(listing_id: int, tenant, data: dict) -> Listing:
        """Обновляет аккаунт и цену листинга с tenant-safe проверками."""
        listing = ListingService.get_for_tenant(listing_id, tenant)
        active_price_only = (
            listing.status == Listing.STATUS_ACTIVE
            and bool(data)
            and set(data) <= {'price_on_listing', 'margin_pct'}
        )
        if listing.status == Listing.STATUS_DELETED or (
            listing.status == Listing.STATUS_ACTIVE and not active_price_only
        ):
            raise InvalidListingStatus(f'Нельзя редактировать листинг в статусе {listing.status}')

        expected = _listing_expected_state(listing)
        update_fields = []
        account_changed = False
        if 'account_id' in data:
            from apps.marketplaces.models import MarketplaceAccount
            try:
                account = MarketplaceAccount.objects.get(
                    pk=data['account_id'],
                    tenant=tenant,
                    is_active=True,
                )
            except MarketplaceAccount.DoesNotExist:
                raise ListingNotFound('Аккаунт Avito не найден')
            exists = Listing.objects.filter(
                tenant=tenant,
                product=listing.product,
                account=account,
            ).exclude(pk=listing.pk).exists()
            if exists:
                raise ListingAccountConflict('Для этого товара уже есть листинг на выбранном аккаунте')
            account_changed = account.pk != listing.account_id
            listing.account = account
            update_fields.append('account')
            if listing.placement_address and listing.placement_address.account_id != account.pk:
                listing.placement_address = None
                update_fields.append('placement_address')

        if 'margin_pct' in data:
            listing.margin_pct = data['margin_pct']
            update_fields.append('margin_pct')
            # Пересчитываем цену от базовой цены товара
            listing.price_on_listing = compute_price(listing.product.price, effective_margin(listing))
            update_fields.append('price_on_listing')
        elif 'price_on_listing' in data:
            listing.price_on_listing = data['price_on_listing']
            update_fields.append('price_on_listing')

        if 'ad_type' in data:
            listing.ad_type = data['ad_type']
            update_fields.append('ad_type')

        if update_fields:
            applied = _save_local_listing_intent(
                listing,
                update_fields,
                reset_provider_identity=account_changed,
                require_target_account_active=account_changed,
                block_provider_owned_pending=account_changed,
                **expected,
            )
            if active_price_only and applied:
                transaction.on_commit(lambda: _enqueue_price_update(listing.pk))
        return listing

    @staticmethod
    def update_placement(listing_id: int, tenant, data: dict) -> Listing:
        """Обновляет адресные override-поля листинга."""
        listing = ListingService.get_for_tenant(listing_id, tenant)
        # Частая ошибка: в поле «ID адреса Avito» вводят external_id аккаунта
        # (он же виден в UI как «ID аккаунта»). Avito такой адрес не находит —
        # отклоняем сразу с понятным пояснением, а не после провала публикации.
        seller_address_id = str(data.get('seller_address_id_override') or '').strip()
        if seller_address_id and seller_address_id == (listing.account.external_id or ''):
            raise InvalidListingStatus(
                'В поле «ID адреса Avito» указан ID аккаунта, а не ID адреса размещения. '
                'Выберите адрес из справочника или укажите корректный ID адреса из профиля Avito.'
            )
        expected = _listing_expected_state(listing)
        update_fields = []
        for field in (
            'address_override',
            'seller_address_id_override',
            'manager_name_override',
            'contact_phone_override',
        ):
            if field in data:
                setattr(listing, field, str(data[field] or '').strip())
                update_fields.append(field)
        if 'placement_address' in data:
            listing.placement_address = _get_placement_address(
                tenant,
                listing.account,
                data.get('placement_address'),
            )
            update_fields.append('placement_address')
        if update_fields:
            _save_local_listing_intent(
                listing,
                update_fields,
                **expected,
            )
        return listing

    @staticmethod
    def bulk_update_placement(tenant, filters: dict, data: dict) -> int:
        """Массово обновляет адресные поля листингов тенанта ниже ручных override."""
        qs = Listing.objects.filter(tenant=tenant)
        listing_ids = filters.get('listing_ids')
        if listing_ids:
            qs = qs.filter(pk__in=listing_ids)
        if filters.get('account_id'):
            qs = qs.filter(account_id=filters['account_id'])
        if filters.get('status'):
            qs = qs.filter(status=filters['status'])
        if filters.get('category_source'):
            qs = qs.filter(product__category_1c=filters['category_source'])
        if filters.get('catalog_category_id'):
            qs = qs.filter(product__catalog_category_id=filters['catalog_category_id'])

        field_map = {
            'address_override': 'bulk_address',
            'seller_address_id_override': 'bulk_seller_address_id',
            'manager_name_override': 'bulk_manager_name',
            'contact_phone_override': 'bulk_contact_phone',
        }
        updates: dict[str, Any] = {}
        for input_field, model_field in field_map.items():
            if input_field in data:
                updates[model_field] = str(data[input_field] or '').strip()
        if 'placement_address' in data:
            address_id = data.get('placement_address')
            if address_id:
                from apps.marketplaces.models import MarketplacePlacementAddress
                try:
                    address = MarketplacePlacementAddress.objects.get(pk=address_id, tenant=tenant, is_active=True)
                except MarketplacePlacementAddress.DoesNotExist:
                    raise ListingNotFound('Адрес размещения не найден')
                qs = qs.filter(account=address.account)
                updates['bulk_placement_address'] = address
            else:
                updates['bulk_placement_address'] = None
        if not updates:
            return 0
        target_ids = list(
            qs.order_by('pk').values_list('pk', flat=True)[:settings.API_BULK_MAX_ITEMS + 1],
        )
        if len(target_ids) > settings.API_BULK_MAX_ITEMS:
            raise ListingBulkLimitExceeded(
                f'Массовая операция допускает не более {settings.API_BULK_MAX_ITEMS} листингов.',
            )
        target_qs = qs.filter(pk__in=target_ids)
        if (
            not _status_lifecycle_dual_write_enabled()
            and not _feed_ingress_dual_write_enabled()
        ):
            return target_qs.update(**updates)

        from apps.marketplaces.models import MarketplaceAccount

        candidate_account_ids = set(
            Listing.objects.filter(pk__in=target_ids)
            .values_list('account_id', flat=True),
        )
        inactive_lifecycle = (
            release_status_check(next_status_check_at=None).as_update_kwargs()
            if _status_lifecycle_dual_write_enabled()
            else {}
        )
        with transaction.atomic():
            locked_accounts = list(
                MarketplaceAccount.all_objects.select_for_update(of=('self',))
                .filter(pk__in=candidate_account_ids)
                .order_by('pk')
                .only('pk')
            )
            locked_account_ids = {account.pk for account in locked_accounts}
            if _feed_ingress_dual_write_enabled():
                from apps.marketplaces.models import MarketplaceFeedEndpoint

                list(
                    MarketplaceFeedEndpoint.objects.select_for_update()
                    .filter(account_id__in=locked_account_ids)
                    .order_by('account_id')
                )
            rows = list(
                Listing.objects.filter(
                    pk__in=target_ids,
                    account_id__in=locked_account_ids,
                )
                .order_by('pk')
                .values(
                    'pk', 'account_id', 'product_id', 'status', 'external_id',
                )
            )
            if _feed_ingress_dual_write_enabled():
                projection_account_ids = {
                    row['account_id'] for row in rows
                    if row['status'] in _FEED_PROJECTION_STATUSES
                }
                if projection_account_ids:
                    from apps.marketplaces.feed_intents import bump_feed_intents

                    bump_feed_intents(
                        projection_account_ids,
                        timezone.now(),
                    )
                projected_product_ids = sorted({
                    row['product_id'] for row in rows
                    if row['status'] in _FEED_PROJECTION_STATUSES
                })
                if projected_product_ids:
                    from apps.products.models import Product

                    list(
                        Product.all_objects.select_for_update()
                        .filter(pk__in=projected_product_ids)
                        .order_by('pk')
                        .values_list('pk', flat=True)
                    )
            locked_ids = [row['pk'] for row in rows]
            list(
                Listing.objects.select_for_update(of=('self',))
                .filter(pk__in=locked_ids)
                .order_by('pk')
                .values_list('pk', flat=True)
            )
            locked_qs = Listing.objects.filter(pk__in=locked_ids)
            updated = locked_qs.update(**updates, **inactive_lifecycle)

            if not _status_lifecycle_dual_write_enabled():
                return updated

            short_due = timezone.now() + _LOCAL_STATUS_RECHECK_DELAY
            active_lifecycle = release_status_check(
                next_status_check_at=short_due,
            ).as_update_kwargs()
            active_ids = [
                row['pk']
                for row in rows
                if row['status'] == Listing.STATUS_ACTIVE
            ]
            archiving_ids = [
                row['pk']
                for row in rows
                if row['status'] == Listing.STATUS_ARCHIVING
                and bool(row['external_id'])
            ]
            locked_qs.filter(pk__in=active_ids).update(**active_lifecycle)
            locked_qs.filter(pk__in=archiving_ids).update(**active_lifecycle)
            due_ids = set(active_ids) | set(archiving_ids)
            due_account_ids = {
                row['account_id'] for row in rows
                if row['pk'] in due_ids
            }
            for account_id in due_account_ids:
                _min_nudge_account_status_due(account_id, short_due)
        return updated

    @staticmethod
    def bulk_action(tenant, data: dict) -> BulkActionResult:
        """Выполняет массовое действие над tenant-scoped листингами."""
        action = data['action']
        listings = list(
            ListingService._bulk_queryset(tenant, data)
            .select_related('tenant', 'product', 'account')
            .order_by('pk')[:settings.API_BULK_MAX_ITEMS + 1]
        )
        if len(listings) > settings.API_BULK_MAX_ITEMS:
            raise ListingBulkLimitExceeded(
                f'Массовая операция допускает не более {settings.API_BULK_MAX_ITEMS} листингов.',
            )
        result: BulkActionResult = {
            'total': len(listings),
            'success': 0,
            'skipped': 0,
            'errors': 0,
            'items': [],
        }

        for listing in listings:
            try:
                if action == 'publish':
                    ListingService.publish(listing.pk, tenant)
                    message = 'Публикация поставлена в очередь'
                elif action == 'archive':
                    ListingService.archive(listing.pk, tenant)
                    message = 'Снятие с публикации поставлено в очередь'
                elif action == 'delete':
                    ListingService.delete(listing.pk, tenant)
                    message = 'Удаление поставлено в очередь'
                elif action == 'update_placement':
                    ListingService.update_placement(listing.pk, tenant, data)
                    message = 'Адрес размещения обновлён'
                else:
                    raise InvalidListingStatus(f'Неизвестное действие: {action}')
            except InvalidListingStatus as exc:
                result['skipped'] += 1
                result['items'].append({
                    'id': listing.pk,
                    'status': 'skipped',
                    'message': str(exc),
                })
                continue
            except ListingNotFound as exc:
                result['errors'] += 1
                result['items'].append({
                    'id': listing.pk,
                    'status': 'error',
                    'message': str(exc),
                })
                continue

            result['success'] += 1
            result['items'].append({
                'id': listing.pk,
                'status': 'ok',
                'message': message,
            })
            ListingService._write_bulk_item_log(tenant, listing, action, message)

        ListingService._write_bulk_log(tenant, action, result)
        return result

    @staticmethod
    def _bulk_queryset(tenant, filters: dict):
        """Возвращает queryset листингов для массового действия с tenant isolation."""
        qs = Listing.objects.filter(tenant=tenant)
        listing_ids = filters.get('listing_ids')
        if listing_ids:
            qs = qs.filter(pk__in=listing_ids)
        if filters.get('account_id'):
            qs = qs.filter(account_id=filters['account_id'])
        if filters.get('status'):
            qs = qs.filter(status=filters['status'])
        return qs

    @staticmethod
    def _write_bulk_log(
        tenant,
        action: str,
        result: BulkActionResult,
    ) -> None:
        """Пишет общий SyncLog по массовому действию."""
        try:
            from apps.sync.models import SyncLog
            status = SyncLog.STATUS_OK if result['errors'] == 0 else SyncLog.STATUS_WARN
            SyncLog.objects.create(
                tenant=tenant,
                event_type=SyncLog.EVENT_LISTING_UPDATE,
                status=status,
                message=(
                    f'Массовое действие listing.{action}: '
                    f'ok={result["success"]}, skipped={result["skipped"]}, errors={result["errors"]}'
                ),
                payload={
                    'action': action,
                    'total': result['total'],
                    'success': result['success'],
                    'skipped': result['skipped'],
                    'errors': result['errors'],
                },
            )
        except Exception:
            pass

    @staticmethod
    def _write_bulk_item_log(tenant, listing: Listing, action: str, message: str) -> None:
        """Пишет SyncLog по конкретному листингу в массовой операции."""
        try:
            from apps.sync.models import SyncLog
            event_map = {
                'publish': SyncLog.EVENT_LISTING_PUBLISH,
                'archive': SyncLog.EVENT_LISTING_UNPUBLISH,
                'delete': SyncLog.EVENT_LISTING_DELETE,
                'update_placement': SyncLog.EVENT_LISTING_UPDATE,
            }
            SyncLog.objects.create(
                tenant=tenant,
                listing=listing,
                product=listing.product,
                event_type=event_map.get(action, SyncLog.EVENT_LISTING_UPDATE),
                status=SyncLog.STATUS_OK,
                message=f'Массовое действие: {message}',
                payload={'action': action},
            )
        except Exception:
            pass

    @staticmethod
    def archive_product(product, tenant) -> int:
        """
        Ставит задачу снятия с публикации для всех активных листингов товара тенанта.

        Возвращает количество затронутых листингов.
        """
        listings = Listing.objects.filter(
            tenant=tenant,
            product=product,
            status=Listing.STATUS_ACTIVE,
        )
        count = 0
        for listing in listings:
            lid = int(listing.pk)
            if not _status_lifecycle_dual_write_enabled():
                transaction.on_commit(partial(_enqueue_unpublish, lid))
                count += 1
                continue
            expected = _listing_expected_state(listing)
            listing.status = Listing.STATUS_ARCHIVING
            if _save_local_listing_intent(
                listing,
                ('status',),
                **expected,
            ):
                transaction.on_commit(partial(_enqueue_unpublish, lid))
                count += 1
        return count

    @staticmethod
    def publish_product(product, tenant) -> list[int]:
        """
        Создаёт или обновляет листинги товара для всех активных аккаунтов тенанта.

        Raises:
            NoActiveAccounts: у тенанта нет активных аккаунтов маркетплейсов.
        """
        from apps.marketplaces.models import MarketplaceAccount
        accounts = MarketplaceAccount.objects.filter(tenant=tenant, is_active=True)
        if not accounts.exists():
            raise NoActiveAccounts('Нет подключённых активных аккаунтов')
        listing_ids = []
        for account in accounts:
            # Со страницы товаров создаём ЧЕРНОВИК, а не публикуем сразу —
            # тенант сначала редактирует цену/контакты и публикует вручную.
            listing = ListingService.create_or_update(product, account, auto_publish=False)
            listing_ids.append(listing.pk)
        return listing_ids

    @staticmethod
    def create_or_update(product, account, change_type: str = 'content', auto_publish: bool = True) -> Listing:
        """
        Создаёт листинг или обновляет существующий в зависимости от change_type.

        price_only → только обновить цену (минимальный запрос к Avito).
        Иначе → полное обновление или первичная публикация.
        auto_publish=False → только создать/обновить черновик, без отправки в Avito
        (тенант публикует вручную из вкладки «Листинги»).
        Задача в Celery ставится через transaction.on_commit — не раньше коммита.
        """
        cat = getattr(product, 'catalog_category', None)
        cat_margin = effective_category_margin(cat) if cat else Decimal('0')
        default_price = compute_price(product.price, cat_margin)

        listing = Listing.all_objects.filter(
            tenant=product.tenant, product=product, account=account,
        ).first()
        if listing is None:
            listing = Listing.objects.create(
                tenant=product.tenant,
                product=product,
                account=account,
                price_on_listing=default_price,
                title=(product.title_ai or product.name)[:300],
                description_ai=product.description_ai,
                status=Listing.STATUS_DRAFT,
            )
        else:
            expected = _listing_expected_state(listing)
            new_price = compute_price(product.price, effective_margin(listing))
            update_fields = ['price_on_listing']
            reset_provider_identity = listing.deleted_at is not None
            if reset_provider_identity:
                listing.deleted_at = None
                listing.status = Listing.STATUS_DRAFT
                update_fields.extend(['deleted_at', 'status', 'updated_at'])
            listing.price_on_listing = new_price
            applied = _save_local_listing_intent(
                listing,
                update_fields,
                reset_provider_identity=reset_provider_identity,
                **expected,
            )
            if auto_publish and applied:
                if change_type == 'price_only':
                    transaction.on_commit(lambda: _enqueue_price_update(listing.pk))
                else:
                    transaction.on_commit(
                        lambda: _enqueue_publish_or_update(listing.pk, False),
                    )
            return listing

        if auto_publish:
            transaction.on_commit(lambda: _enqueue_publish_or_update(listing.pk, True))
        return listing


class MarketplaceAccountService:
    """Сервис управления аккаунтами маркетплейсов: создание, обновление credentials."""

    @staticmethod
    def _fetch_avito_user_id(credentials_enc: bytes) -> str:
        """Получает числовой user_id из Avito API по credentials."""
        import requests as req
        from apps.core.http_responses import bounded_http_request
        from apps.marketplaces.adapters.avito.auth import AvitoAuthManager

        class _Tmp:
            pk: None = None
            credentials_enc: bytes

        tmp = _Tmp()
        tmp.credentials_enc = credentials_enc
        try:
            token = AvitoAuthManager()._refresh(tmp)
            resp = bounded_http_request(
                req.get,
                'https://api.avito.ru/core/v1/accounts/self',
                headers={'Authorization': f'Bearer {token}'},
                timeout=10,
                max_bytes=settings.AVITO_API_RESPONSE_MAX_BYTES,
            )
            resp.raise_for_status()
            user_id = resp.json().get('id')
        except Exception:
            raise InvalidMarketplaceCredentials('Не удалось проверить Avito API-ключи. Проверьте их правильность.')
        if not user_id:
            raise InvalidMarketplaceCredentials('Avito API не вернул user_id аккаунта')
        return str(user_id)

    @staticmethod
    def create(tenant, data: dict):
        """
        Создаёт аккаунт маркетплейса с зашифрованными credentials.

        Автоматически запрашивает реальный Avito user_id через API.
        При конфликте external_id бросает AccountAlreadyExists.
        """
        from apps.marketplaces.models import MarketplaceAccount

        if (
            data.get('marketplace') == MarketplaceAccount.MARKETPLACE_AVITO
            and getattr(
                settings,
                'MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED',
                False,
            )
        ):
            raise MarketplaceAccountFeedConflict(
                'Подключение новых Avito-аккаунтов временно приостановлено '
                'на время миграции Autoload-профилей.',
            )
        from apps.datasources.encryption import encrypt
        credentials_enc = encrypt({
            'client_id': data['client_id'],
            'client_secret': data['client_secret'],
        })
        external_id = MarketplaceAccountService._fetch_avito_user_id(credentials_enc)
        deleted_account = MarketplaceAccount.all_objects.filter(
            tenant=tenant,
            marketplace=data['marketplace'],
            external_id=external_id,
            deleted_at__isnull=False,
        ).first()
        if deleted_account is not None:
            with transaction.atomic():
                account = (
                    MarketplaceAccount.all_objects.select_for_update()
                    .get(pk=deleted_account.pk)
                )
                feed_endpoint = _lock_marketplace_feed_endpoint(account.pk)
                from apps.marketplaces.feed_workflow import account_identity_digest
                previous_generation = account_identity_digest(account)
                account.name = data['name']
                account.credentials_enc = credentials_enc
                account.is_active = True
                account.deleted_at = None
                generation_changed = not hmac.compare_digest(
                    previous_generation,
                    account_identity_digest(account),
                )
                new_endpoint_rekey = bool(
                    generation_changed
                    and feed_endpoint is not None
                    and feed_endpoint.profile_state
                    == feed_endpoint.ProfileState.NEW
                    and not feed_endpoint.serve_enabled
                )
                if generation_changed:
                    if not new_endpoint_rekey:
                        _assert_feed_endpoint_identity_mutation_safe(feed_endpoint)
                    _assert_account_identity_mutation_safe(account)
                account.save(update_fields=(
                    'name', 'credentials_enc', 'is_active', 'deleted_at',
                    'updated_at',
                ))
                if generation_changed:
                    if new_endpoint_rekey:
                        _refresh_new_feed_endpoint_identity(account, feed_endpoint)
                    else:
                        _fence_marketplace_feed_endpoint_identity(
                            account,
                            feed_endpoint,
                        )
                _invalidate_avito_access_token_after_commit(account)
                if private_feed_fleet_enabled():
                    from apps.marketplaces.feed_profile_migration import (
                        FeedProfileMigrationError,
                        ensure_fleet_feed_endpoint,
                    )
                    try:
                        ensure_fleet_feed_endpoint(account)
                    except FeedProfileMigrationError as exc:
                        raise MarketplaceAccountFeedConflict(
                            'Не удалось безопасно подготовить защищённый '
                            'фид Avito.',
                        ) from exc
        else:
            try:
                with transaction.atomic():
                    account = MarketplaceAccount.objects.create(
                        tenant=tenant,
                        name=data['name'],
                        marketplace=data['marketplace'],
                        external_id=external_id,
                        credentials_enc=credentials_enc,
                    )
                    if private_feed_fleet_enabled():
                        from apps.marketplaces.feed_profile_migration import (
                            FeedProfileMigrationError,
                            ensure_fleet_feed_endpoint,
                        )
                        try:
                            ensure_fleet_feed_endpoint(account)
                        except FeedProfileMigrationError as exc:
                            raise MarketplaceAccountFeedConflict(
                                'Не удалось безопасно подготовить защищённый '
                                'фид Avito.',
                            ) from exc
            except IntegrityError:
                existing_id = (
                    MarketplaceAccount.objects.filter(
                        tenant=tenant,
                        marketplace=data['marketplace'],
                        external_id=external_id,
                    ).values_list('pk', flat=True).first()
                )
                raise AccountAlreadyExists(
                    'Аккаунт с таким external_id уже существует',
                    account_id=existing_id,
                )

        # Регистрируем feed URL в Avito Autoload после коммита транзакции
        if account.marketplace == MarketplaceAccount.MARKETPLACE_AVITO:
            from apps.marketplaces.autoload_onboarding import (
                schedule_autoload_profile_setup,
            )
            transaction.on_commit(
                partial(
                    schedule_autoload_profile_setup,
                    account.pk,
                    account.tenant_id,
                ),
            )

        return account

    @staticmethod
    def update_credentials(account, data: dict):
        """Полностью обновляет аккаунт: имя, marketplace, external_id и credentials."""
        from apps.datasources.encryption import encrypt
        credentials_enc = encrypt({
            'client_id': data['client_id'],
            'client_secret': data['client_secret'],
        })
        external_id = MarketplaceAccountService._fetch_avito_user_id(credentials_enc)
        try:
            with transaction.atomic():
                account = (
                    type(account).all_objects.select_for_update()
                    .get(pk=account.pk)
                )
                feed_endpoint = _lock_marketplace_feed_endpoint(account.pk)
                from apps.marketplaces.feed_workflow import account_identity_digest
                previous_generation = account_identity_digest(account)
                previous_identity = (account.marketplace, account.external_id)
                account.name = data['name']
                account.marketplace = data['marketplace']
                account.external_id = external_id
                account.credentials_enc = credentials_enc
                generation_changed = not hmac.compare_digest(
                    previous_generation,
                    account_identity_digest(account),
                )
                new_endpoint_rekey = bool(
                    generation_changed
                    and feed_endpoint is not None
                    and feed_endpoint.profile_state
                    == feed_endpoint.ProfileState.NEW
                    and not feed_endpoint.serve_enabled
                )
                if generation_changed:
                    if not new_endpoint_rekey:
                        _assert_feed_endpoint_identity_mutation_safe(feed_endpoint)
                    _assert_account_identity_mutation_safe(account)
                identity_changed = previous_identity != (
                    account.marketplace, account.external_id,
                )
                account_lifecycle_fields: tuple[str, ...] = ()
                if identity_changed:
                    account_lifecycle_fields = _reset_account_status_batch(account)
                elif _status_lifecycle_dual_write_enabled():
                    account.status_batch_claim_token = None
                    account.status_batch_claimed_until = None
                    account.status_batch_cooldown_until = None
                    account_lifecycle_fields = (
                        'status_batch_claim_token',
                        'status_batch_claimed_until',
                        'status_batch_cooldown_until',
                    )
                account.save(update_fields=_merged_update_fields(
                    (
                        'name', 'marketplace', 'external_id',
                        'credentials_enc', 'updated_at',
                    ),
                    account_lifecycle_fields,
                ))
                if generation_changed:
                    if new_endpoint_rekey:
                        _refresh_new_feed_endpoint_identity(account, feed_endpoint)
                    else:
                        _fence_marketplace_feed_endpoint_identity(
                            account,
                            feed_endpoint,
                        )
                _invalidate_avito_access_token_after_commit(account)
                if generation_changed:
                    _bump_account_feed_projection_if_live(account.pk)
                if identity_changed:
                    Listing.all_objects.filter(account=account).update(
                        **_provider_identity_reset_kwargs(),
                    )
                if new_endpoint_rekey:
                    from apps.marketplaces.autoload_onboarding import (
                        schedule_autoload_profile_setup,
                    )
                    transaction.on_commit(partial(
                        schedule_autoload_profile_setup,
                        account.pk,
                        account.tenant_id,
                    ))
        except IntegrityError:
            existing_id = (
                type(account).objects.filter(
                    tenant_id=account.tenant_id,
                    marketplace=data['marketplace'],
                    external_id=external_id,
                ).exclude(pk=account.pk).values_list('pk', flat=True).first()
            )
            raise AccountAlreadyExists(
                'Аккаунт с таким external_id уже существует',
                account_id=existing_id,
            )
        return account

    @staticmethod
    def update_partial(account, data: dict):
        """Частично обновляет аккаунт: is_active, name и настройки размещения."""
        from django.db.models import Min

        with transaction.atomic():
            account = (
                type(account).all_objects.select_for_update()
                .get(pk=account.pk)
            )
            update_fields = []
            was_active = account.is_active
            feed_defaults_changed = False
            if 'is_active' in data:
                account.is_active = bool(data['is_active'])
                update_fields.append('is_active')
            if 'name' in data:
                account.name = str(data['name'])[:200]
                update_fields.append('name')
            for field in (
                'default_address',
                'default_seller_address_id',
                'default_manager_name',
                'default_contact_phone',
            ):
                if field in data:
                    value = str(data[field] or '').strip()
                    if getattr(account, field) != value:
                        feed_defaults_changed = True
                    setattr(account, field, value)
                    update_fields.append(field)
            if 'autoload_subscription_ends_at' in data:
                account.autoload_subscription_ends_at = data['autoload_subscription_ends_at']
                update_fields.append('autoload_subscription_ends_at')

            if update_fields:
                _assert_legacy_feed_cursor_mutation_safe(account)

            reactivated = not was_active and account.is_active
            feed_endpoint = None
            if (
                (was_active and not account.is_active)
                or (
                    _feed_ingress_dual_write_enabled()
                    and (feed_defaults_changed or reactivated)
                )
            ):
                feed_endpoint = _lock_marketplace_feed_endpoint(account.pk)

            if _feed_ingress_dual_write_enabled() and reactivated:
                from apps.products.models import Product

                product_ids = list(
                    Listing.objects.filter(
                        account_id=account.pk,
                        status__in=_FEED_PROJECTION_STATUSES,
                    )
                    .order_by('product_id')
                    .values_list('product_id', flat=True)
                    .distinct()
                )
                list(
                    Product.all_objects.select_for_update()
                    .filter(pk__in=product_ids)
                    .order_by('pk')
                    .values_list('pk', flat=True)
                )

            if was_active and not account.is_active:
                _assert_feed_endpoint_availability_mutation_safe(feed_endpoint)
                from apps.marketplaces.feed_workflow import (
                    OWNER_CHANGE_HOLD_SUBMITTED,
                    fence_account_feed_runs_for_owner_change,
                )

                fence_account_feed_runs_for_owner_change(
                    account.pk,
                    reason='Marketplace account was deactivated.',
                    safe_state=MarketplaceFeedRun.State.CANCELLED,
                    submitted_policy=OWNER_CHANGE_HOLD_SUBMITTED,
                )

            if _status_lifecycle_dual_write_enabled() and was_active != account.is_active:
                account.status_batch_claim_token = None
                account.status_batch_claimed_until = None
                account.status_batch_cooldown_until = None
                update_fields.extend([
                    'status_batch_claim_token', 'status_batch_claimed_until',
                    'status_batch_cooldown_until',
                ])
                if account.is_active:
                    account.status_batch_due_at = (
                        Listing.objects.filter(
                            account_id=account.pk,
                            external_id__isnull=False,
                            next_status_check_at__isnull=False,
                        )
                        .exclude(external_id='')
                        .aggregate(value=Min('next_status_check_at'))['value']
                    )
                else:
                    account.status_batch_due_at = None
                update_fields.append('status_batch_due_at')
            if update_fields:
                account.save(update_fields=_merged_update_fields(
                    update_fields,
                    ('updated_at',),
                ))

            if (
                _feed_ingress_dual_write_enabled()
                and (feed_defaults_changed or reactivated)
            ):
                _bump_account_feed_projection_if_live(account.pk)

            has_live_feed_owner = (
                reactivated
                and _durable_feed_run_enabled(account.pk)
                and account.marketplace == account.MARKETPLACE_AVITO
                and type(account).objects.filter(
                    pk=account.pk,
                    is_active=True,
                    tenant__is_active=True,
                ).exists()
                and not MarketplaceFeedRun.objects.filter(
                    account_id=account.pk,
                    state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
                ).exists()
            )
            if has_live_feed_owner and Listing.objects.filter(
                account_id=account.pk,
                status=Listing.STATUS_PENDING,
                external_id__isnull=True,
            ).exists():
                from apps.marketplaces.tasks import request_feed_flush

                transaction.on_commit(partial(request_feed_flush, account))
        return account


class AvitoAccountStatusService:
    """Синхронизирует подтверждённое состояние профиля и тарифа Avito."""

    @staticmethod
    def _timestamp(value):
        """Преобразует Unix timestamp Avito в timezone-aware datetime."""
        if value in (None, ''):
            return None
        try:
            return datetime.datetime.fromtimestamp(
                int(value), tz=datetime.timezone.utc,
            )
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _price(contract: dict):
        """Возвращает стоимость тарифа как Decimal либо None."""
        value = (contract.get('price') or {}).get('price')
        if value in (None, ''):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None

    @staticmethod
    def _packages(contract: dict) -> list[dict]:
        """Оставляет только безопасные tenant-facing поля пакетов размещений."""
        result = []
        for package in contract.get('packages') or []:
            if not isinstance(package, dict):
                continue
            result.append({
                'categories': package.get('categories') or [],
                'locations': package.get('locations') or [],
                'remain': package.get('remain'),
                'total': package.get('total'),
            })
        return result

    @classmethod
    def _apply_tariff(cls, status_obj: AvitoAccountStatus, payload: dict, checked_at) -> None:
        """Сохраняет нормализованный текущий и следующий тариф."""
        current = payload.get('current') or {}
        if current:
            status_obj.tariff_status = (
                AvitoAccountStatus.TARIFF_ACTIVE
                if current.get('isActive')
                else AvitoAccountStatus.TARIFF_INACTIVE
            )
            status_obj.tariff_name = str(current.get('level') or '')[:200]
            status_obj.tariff_started_at = cls._timestamp(current.get('startTime'))
            status_obj.tariff_ends_at = cls._timestamp(current.get('closeTime'))
            status_obj.tariff_price = cls._price(current)
            status_obj.placement_packages = cls._packages(current)
        else:
            status_obj.tariff_status = AvitoAccountStatus.TARIFF_NOT_FOUND
            status_obj.tariff_name = ''
            status_obj.tariff_started_at = None
            status_obj.tariff_ends_at = None
            status_obj.tariff_price = None
            status_obj.placement_packages = []

        scheduled = payload.get('scheduled') or {}
        status_obj.scheduled_tariff = {
            'name': str(scheduled.get('level') or '')[:200],
            'starts_at': (
                cls._timestamp(scheduled.get('startTime')).isoformat()
                if cls._timestamp(scheduled.get('startTime'))
                else None
            ),
            'price': (
                str(cls._price(scheduled))
                if cls._price(scheduled) is not None
                else None
            ),
        } if scheduled else {}
        status_obj.tariff_checked_at = checked_at

    @staticmethod
    def _days_left(status_obj: AvitoAccountStatus) -> int | None:
        """Возвращает дни по API-тарифу или по ручной дате Autoload."""
        if status_obj.tariff_ends_at:
            seconds_left = (status_obj.tariff_ends_at - timezone.now()).total_seconds()
            if seconds_left <= 0:
                return 0
            return int((seconds_left + 86399) // 86400)
        manual_end = status_obj.account.autoload_subscription_ends_at
        if not manual_end:
            return None
        return max((manual_end - timezone.localdate()).days, 0)

    @staticmethod
    def _period_key(status_obj: AvitoAccountStatus) -> str:
        if status_obj.tariff_ends_at:
            return f'api:{status_obj.tariff_ends_at.isoformat()}'
        if status_obj.account.autoload_subscription_ends_at:
            return f'manual:{status_obj.account.autoload_subscription_ends_at.isoformat()}'
        return ''

    @staticmethod
    def _queue_notification(status_obj: AvitoAccountStatus, level: str, message: str) -> None:
        """Отправляет уведомление после фиксации снимка в транзакции."""
        from apps.notifications.tasks import send_notification_task

        transaction.on_commit(
            lambda: send_notification_task.delay(
                status_obj.tenant_id, level, message,
                {'account_id': status_obj.account_id},
            )
        )

    @classmethod
    def _notify_thresholds(cls, status_obj: AvitoAccountStatus) -> None:
        """Дедуплицированно уведомляет о сроке, лимите и отключении Autoload."""
        from apps.notifications.services import LEVEL_CRITICAL, LEVEL_ERROR

        state = dict(status_obj.notification_state or {})
        period_key = cls._period_key(status_obj)
        if state.get('period') != period_key:
            onboarding_state = state.get('autoload_onboarding')
            state = {'period': period_key}
            if isinstance(onboarding_state, dict):
                state['autoload_onboarding'] = onboarding_state

        if (
            status_obj.connection_status
            == AvitoAccountStatus.CONNECTION_AUTH_ERROR
            and state.get('connection') != AvitoAccountStatus.CONNECTION_AUTH_ERROR
        ):
            cls._queue_notification(
                status_obj,
                LEVEL_CRITICAL,
                f'Avito ({status_obj.account.name}): ключи доступа отклонены. '
                'Переподключите аккаунт.',
            )
            state['connection'] = AvitoAccountStatus.CONNECTION_AUTH_ERROR
        elif status_obj.connection_status == AvitoAccountStatus.CONNECTION_CONNECTED:
            state.pop('connection', None)

        days_left = cls._days_left(status_obj)
        if (
            status_obj.autoload_status == AvitoAccountStatus.AUTOLOAD_ENABLED
            and days_left is not None
        ):
            expiry_threshold = next(
                (threshold for threshold in (0, 1, 3, 7, 14) if days_left <= threshold),
                None,
            )
            if expiry_threshold is not None and state.get('expiry') != expiry_threshold:
                level = LEVEL_CRITICAL if days_left <= 1 else LEVEL_ERROR
                if status_obj.tariff_ends_at:
                    expiry_message = (
                        f'Avito ({status_obj.account.name}): до окончания '
                        f'тарифа осталось {days_left} дн.'
                    )
                else:
                    manual_end = status_obj.account.autoload_subscription_ends_at
                    if manual_end and manual_end < timezone.localdate():
                        expiry_message = (
                            f'Avito ({status_obj.account.name}): указанная вручную '
                            'дата окончания Автозагрузки '
                            f'{manual_end:%d.%m.%Y} уже прошла. '
                            'Проверьте актуальный срок в Avito.'
                        )
                    elif days_left == 0:
                        expiry_message = (
                            f'Avito ({status_obj.account.name}): указанная вручную '
                            'дата окончания Автозагрузки — сегодня.'
                        )
                    else:
                        expiry_message = (
                            f'Avito ({status_obj.account.name}): по указанной вручную '
                            'дате до окончания Автозагрузки '
                            f'осталось {days_left} дн.'
                        )
                cls._queue_notification(
                    status_obj,
                    level,
                    expiry_message,
                )
                state['expiry'] = expiry_threshold

        if (
            status_obj.tariff_status == AvitoAccountStatus.TARIFF_INACTIVE
            and state.get('tariff') != AvitoAccountStatus.TARIFF_INACTIVE
        ):
            cls._queue_notification(
                status_obj,
                LEVEL_CRITICAL,
                f'Avito ({status_obj.account.name}): тариф неактивен.',
            )
            state['tariff'] = AvitoAccountStatus.TARIFF_INACTIVE
        elif status_obj.tariff_status == AvitoAccountStatus.TARIFF_ACTIVE:
            state.pop('tariff', None)

        remaining: list[int] = []
        totals: list[int] = []
        for package in status_obj.placement_packages:
            if not isinstance(package, dict):
                continue
            remain = package.get('remain')
            total = package.get('total')
            if isinstance(remain, int):
                remaining.append(remain)
            if isinstance(total, int):
                totals.append(total)
        if remaining and totals and sum(totals) > 0:
            percent_left = int(sum(remaining) * 100 / sum(totals))
            limit_threshold = next(
                (threshold for threshold in (0, 10, 20) if percent_left <= threshold),
                None,
            )
            if limit_threshold is not None and state.get('placements') != limit_threshold:
                cls._queue_notification(
                    status_obj,
                    LEVEL_CRITICAL if percent_left == 0 else LEVEL_ERROR,
                    f'Avito ({status_obj.account.name}): осталось '
                    f'{sum(remaining)} размещений из {sum(totals)}.',
                )
                state['placements'] = limit_threshold

        if status_obj.autoload_status in {
            AvitoAccountStatus.AUTOLOAD_DISABLED,
            AvitoAccountStatus.AUTOLOAD_MISSING,
            AvitoAccountStatus.AUTOLOAD_FORBIDDEN,
        } and state.get('autoload') != status_obj.autoload_status:
            cls._queue_notification(
                status_obj,
                LEVEL_CRITICAL,
                f'Avito ({status_obj.account.name}): Автозагрузка недоступна '
                f'({dict(AvitoAccountStatus.AUTOLOAD_CHOICES).get(status_obj.autoload_status)}).',
            )
            state['autoload'] = status_obj.autoload_status
        elif status_obj.autoload_status == AvitoAccountStatus.AUTOLOAD_ENABLED:
            state.pop('autoload', None)

        if state != status_obj.notification_state:
            status_obj.notification_state = state
            status_obj.save(update_fields=['notification_state', 'updated_at'])

    @classmethod
    def refresh(cls, account) -> AvitoAccountStatus:
        """
        Обновляет снимок аккаунта, не стирая подтверждённые данные при временном сбое.

        Отсутствие тарифа и профиля — подтверждённые ответы Avito. Таймауты,
        rate limit и 5xx сохраняются только как ошибка последней попытки.
        """
        from requests import RequestException

        from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
        from apps.marketplaces.adapters.avito.error_handler import (
            ForbiddenError,
            NotFoundError,
            ServerError,
            TokenExpiredError,
        )
        from apps.marketplaces.adapters.avito.rate_limiter import RateLimitError

        status_obj, _ = AvitoAccountStatus.objects.get_or_create(
            tenant=account.tenant,
            account=account,
        )
        checked_at = timezone.now()
        status_obj.last_attempted_at = checked_at
        errors = []
        adapter = AvitoAdapter(account)

        try:
            profile = adapter.get_autoload_profile()
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_CONNECTED
            status_obj.autoload_status = (
                AvitoAccountStatus.AUTOLOAD_ENABLED
                if profile.get('autoload_enabled')
                else AvitoAccountStatus.AUTOLOAD_DISABLED
            )
            from apps.marketplaces.models import MarketplaceFeedEndpoint
            endpoint = (
                MarketplaceFeedEndpoint.objects.select_related(
                    'account', 'account__tenant',
                )
                .filter(account_id=account.pk)
                .first()
            )
            if endpoint is None:
                expected_feed_url = adapter._feed_public_url()
                status_obj.feed_configured = any(
                    feed.get('feed_url') == expected_feed_url
                    for feed in (profile.get('feeds_data') or [])
                    if isinstance(feed, dict)
                )
            else:
                from apps.marketplaces.adapters.avito.profile_migration import (
                    is_profile_feed_configured,
                )
                status_obj.feed_configured = is_profile_feed_configured(
                    endpoint=endpoint,
                    profile=profile,
                )
            status_obj.profile_checked_at = checked_at
        except NotFoundError:
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_CONNECTED
            status_obj.autoload_status = AvitoAccountStatus.AUTOLOAD_MISSING
            status_obj.feed_configured = False
            status_obj.profile_checked_at = checked_at
        except ForbiddenError:
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_CONNECTED
            status_obj.autoload_status = AvitoAccountStatus.AUTOLOAD_FORBIDDEN
            status_obj.feed_configured = None
            status_obj.profile_checked_at = checked_at
        except TokenExpiredError:
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_AUTH_ERROR
            errors.append(('auth_error', 'Avito отклонил ключи доступа'))
        except (RateLimitError, ServerError, RequestException):
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_UNAVAILABLE
            errors.append(('profile_unavailable', 'Не удалось обновить профиль Автозагрузки'))
        except Exception:
            status_obj.connection_status = AvitoAccountStatus.CONNECTION_UNAVAILABLE
            errors.append(('profile_unavailable', 'Не удалось обновить профиль Автозагрузки'))

        if status_obj.connection_status != AvitoAccountStatus.CONNECTION_AUTH_ERROR:
            try:
                cls._apply_tariff(status_obj, adapter.get_tariff_info(), checked_at)
            except NotFoundError:
                cls._apply_tariff(status_obj, {}, checked_at)
            except ForbiddenError:
                cls._apply_tariff(status_obj, {}, checked_at)
            except TokenExpiredError:
                status_obj.connection_status = AvitoAccountStatus.CONNECTION_AUTH_ERROR
                errors.append(('auth_error', 'Avito отклонил ключи доступа'))
            except (RateLimitError, ServerError, RequestException):
                errors.append(('tariff_unavailable', 'Не удалось обновить тариф Avito'))
            except Exception:
                errors.append(('tariff_unavailable', 'Не удалось обновить тариф Avito'))

        if errors:
            status_obj.last_error_code, status_obj.last_error_message = errors[-1]
        else:
            status_obj.last_error_code = ''
            status_obj.last_error_message = ''
        status_obj.save()

        account.autoload_active = {
            AvitoAccountStatus.AUTOLOAD_ENABLED: True,
            AvitoAccountStatus.AUTOLOAD_DISABLED: False,
            AvitoAccountStatus.AUTOLOAD_MISSING: False,
            AvitoAccountStatus.AUTOLOAD_FORBIDDEN: False,
        }.get(status_obj.autoload_status)
        if status_obj.profile_checked_at:
            account.autoload_checked_at = status_obj.profile_checked_at
        account.save(update_fields=['autoload_active', 'autoload_checked_at'])
        cls._notify_thresholds(status_obj)
        return status_obj


def _enqueue_publish_or_update(listing_id: int, is_new: bool) -> None:
    """Ставит задачу публикации или обновления листинга в Celery."""
    from apps.marketplaces.tasks import publish_listing_task, update_listing_task
    if is_new:
        publish_listing_task.delay(listing_id)
    else:
        update_listing_task.delay(listing_id)


class StatsService:
    """Сервис получения и сохранения ежедневной статистики листингов с Avito."""

    MAX_HISTORY_DAYS = 270
    MAX_COUNTER_VALUE = (1 << 31) - 1

    @classmethod
    def _counter(cls, value: object) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return min(max(value, 0), cls.MAX_COUNTER_VALUE)
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            if len(value) > 10:
                return cls.MAX_COUNTER_VALUE
            return min(int(value), cls.MAX_COUNTER_VALUE)
        return 0

    @staticmethod
    def _day(value: object) -> datetime.date | None:
        if not isinstance(value, str):
            return None
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _fetch_raw(account, item_ids: list[str], date_from: datetime.date, date_to: datetime.date) -> list[dict]:
        """
        Вызывает AvitoAdapter.get_stats и возвращает сырой ответ API.

        Вынесен отдельным методом для удобного mock-а в тестах.
        """
        from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
        return AvitoAdapter(account).get_stats(item_ids, date_from, date_to)

    @classmethod
    def fetch_for_account(
        cls,
        account,
        date_from: datetime.date,
        date_to: datetime.date,
    ) -> int:
        """
        Получает статистику активных листингов аккаунта за период и сохраняет в ListingStats.

        Использует bulk_create с update_conflicts — идемпотентен при повторном вызове.
        Возвращает количество обработанных записей (не уникальных: один листинг × N дней).
        """
        if date_from > date_to:
            raise ValueError('Statistics date_from must not exceed date_to.')
        if (date_to - date_from).days >= cls.MAX_HISTORY_DAYS:
            raise ValueError('Statistics range must not exceed 270 days.')

        listings = list(
            Listing.objects.filter(
                account=account,
                status=Listing.STATUS_ACTIVE,
                external_id__isnull=False,
            ).values('id', 'external_id', 'tenant_id')
        )
        if not listings:
            return 0

        from apps.marketplaces.adapters.avito.adapter import (
            normalize_avito_stats_item_id,
        )

        listing_by_external: dict[str, dict] = {}
        ambiguous_ids: set[str] = set()
        for item in listings:
            external_id = normalize_avito_stats_item_id(item['external_id'])
            if external_id is None or external_id in ambiguous_ids:
                continue
            if external_id in listing_by_external:
                listing_by_external.pop(external_id, None)
                ambiguous_ids.add(external_id)
                continue
            listing_by_external[external_id] = item
        if not listing_by_external:
            return 0
        raw = cls._fetch_raw(account, list(listing_by_external.keys()), date_from, date_to)

        by_listing_day: dict[tuple[int, datetime.date], ListingStats] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            external_id = normalize_avito_stats_item_id(item.get('itemId'))
            info = listing_by_external.get(external_id or '')
            if not info:
                continue
            days = item.get('stats')
            if not isinstance(days, list):
                continue
            for day in days:
                if not isinstance(day, dict):
                    continue
                stat_date = cls._day(day.get('date'))
                if (
                    stat_date is None
                    or stat_date < date_from
                    or stat_date > date_to
                ):
                    continue
                # uniqViews — уникальные просмотры карточки (views в нашей модели)
                # views — все просмотры, используем как прокси для показов (impressions)
                # uniqContacts — уникальные контакты (contacts в нашей модели)
                views = cls._counter(day.get('uniqViews', 0))
                impressions = cls._counter(day.get('views', 0))
                contacts = cls._counter(day.get('uniqContacts', 0))
                ctr = round(views / impressions * 100, 2) if impressions else 0.0
                by_listing_day[(info['id'], stat_date)] = ListingStats(
                    listing_id=info['id'],
                    tenant_id=info['tenant_id'],
                    date=stat_date,
                    views=views,
                    impressions=impressions,
                    contacts=contacts,
                    ctr=ctr,
                )

        to_upsert = list(by_listing_day.values())
        if not to_upsert:
            return 0

        ListingStats.objects.bulk_create(
            to_upsert,
            update_conflicts=True,
            unique_fields=['listing_id', 'date'],
            update_fields=['views', 'impressions', 'contacts', 'ctr'],
        )
        return len(to_upsert)


def _get_placement_address(tenant, account, address_id):
    """Возвращает активный адрес tenant-а для конкретного аккаунта или None."""
    if not address_id:
        return None
    from apps.marketplaces.models import MarketplacePlacementAddress
    try:
        return MarketplacePlacementAddress.objects.get(
            pk=address_id,
            tenant=tenant,
            account=account,
            is_active=True,
        )
    except MarketplacePlacementAddress.DoesNotExist:
        raise ListingNotFound('Адрес размещения не найден')


def _enqueue_price_update(listing_id: int) -> None:
    """Ставит задачу обновления цены листинга в Celery."""
    from apps.marketplaces.tasks import update_price_task
    update_price_task.delay(listing_id)


def _enqueue_ai_generation(product_id: int) -> None:
    """Ставит enrichment-aware задачу генерации AI-описания в Celery."""
    from apps.products.models import Product
    from apps.products.services import ProductService

    product = Product.objects.select_related('tenant').get(pk=product_id)
    ProductService.schedule_ai_generation(product, product.tenant)


def _enqueue_unpublish(listing_id: int) -> None:
    """Ставит задачу снятия листинга с публикации в Celery."""
    from apps.marketplaces.tasks import unpublish_listing_task
    unpublish_listing_task.delay(listing_id)


def _enqueue_delete(listing_id: int) -> None:
    """Ставит задачу удаления листинга в Celery."""
    from apps.marketplaces.tasks import delete_listing_task
    delete_listing_task.delay(listing_id)


def _enqueue_poll_feed_results(account_id: int) -> None:
    """Ставит ручную проверку результатов Avito feed в Celery."""
    from apps.marketplaces.tasks import poll_feed_results_task
    poll_feed_results_task.delay(account_id)


def _enqueue_provider_listing_status_check(
    listing_id: int,
    *,
    is_archiving: bool,
) -> None:
    """Ставит точечную проверку уже известного объявления Avito."""

    from apps.marketplaces.tasks import (
        check_moderation_task,
        confirm_removal_task,
    )

    task = confirm_removal_task if is_archiving else check_moderation_task
    task.delay(listing_id)
