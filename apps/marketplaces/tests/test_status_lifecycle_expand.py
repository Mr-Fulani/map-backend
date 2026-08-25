from decimal import Decimal

import pytest
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.models.fields import NOT_PROVIDED

from apps.marketplaces.models import Listing, MarketplaceAccount
from apps.products.models import Product
from apps.tenants.models import Tenant


LISTING_EXPAND_FIELDS = (
    'remote_status',
    'remote_status_checked_at',
    'next_status_check_at',
    'status_check_claim_token',
    'status_check_claimed_until',
)
ACCOUNT_EXPAND_FIELDS = (
    'status_batch_due_at',
    'status_batch_cooldown_until',
    'status_batch_claim_token',
    'status_batch_claimed_until',
)

LISTING_DUE_INDEX = 'mkt_lst_acct_stat_due'
ACCOUNT_DUE_INDEX = 'mkt_acct_provider_due'

LISTING_DUE_CONDITION = (
    models.Q(
        deleted_at__isnull=True,
        external_id__isnull=False,
        next_status_check_at__isnull=False,
    )
    & ~models.Q(external_id='')
)

ACCOUNT_DUE_CONDITION = models.Q(
    deleted_at__isnull=True,
    is_active=True,
    status_batch_due_at__isnull=False,
)


def _marketplaces_migration(loader, number):
    matches = [
        migration
        for (app_label, name), migration in loader.disk_migrations.items()
        if app_label == 'marketplaces' and name.startswith(f'{number}_')
    ]
    assert len(matches) == 1, (
        f'Expected one marketplaces migration {number}, got {matches!r}'
    )
    return matches[0]


def _index(model, name):
    matches = [index for index in model._meta.indexes if index.name == name]
    assert len(matches) == 1
    return matches[0]


def test_status_lifecycle_expand_model_fields_are_inert_and_nullable():
    for model, field_names in (
        (Listing, LISTING_EXPAND_FIELDS),
        (MarketplaceAccount, ACCOUNT_EXPAND_FIELDS),
    ):
        for field_name in field_names:
            field = model._meta.get_field(field_name)
            assert field.null is True, f'{model.__name__}.{field_name}'
            assert field.blank is True, f'{model.__name__}.{field_name}'
            assert field.editable is False, f'{model.__name__}.{field_name}'
            assert field.default is NOT_PROVIDED, f'{model.__name__}.{field_name}'
            assert field.has_default() is False, f'{model.__name__}.{field_name}'
            assert field.db_index is False, f'{model.__name__}.{field_name}'

    remote_status = Listing._meta.get_field('remote_status')
    assert remote_status.max_length == 32
    assert list(remote_status.choices) == Listing.REMOTE_STATUS_CHOICES

    listing_index = _index(Listing, LISTING_DUE_INDEX)
    assert listing_index.fields == [
        'account', 'status', 'next_status_check_at', 'id',
    ]
    assert listing_index.condition == LISTING_DUE_CONDITION

    account_index = _index(MarketplaceAccount, ACCOUNT_DUE_INDEX)
    assert account_index.fields == [
        'marketplace', 'status_batch_due_at', 'id',
    ]
    assert account_index.condition == ACCOUNT_DUE_CONDITION


@pytest.mark.django_db
def test_status_lifecycle_expand_migrations_are_additive_and_split():
    loader = MigrationLoader(connection)
    expand = _marketplaces_migration(loader, '0020')
    listing_index_migration = _marketplaces_migration(loader, '0021')
    account_index_migration = _marketplaces_migration(loader, '0022')

    assert expand.atomic is True
    assert all(
        isinstance(operation, migrations.AddField)
        for operation in expand.operations
    )
    expected_fields = {
        *{('listing', name) for name in LISTING_EXPAND_FIELDS},
        *{('marketplaceaccount', name) for name in ACCOUNT_EXPAND_FIELDS},
    }
    assert {
        (operation.model_name, operation.name)
        for operation in expand.operations
    } == expected_fields
    for operation in expand.operations:
        field = operation.field
        assert field.null is True, f'{operation.model_name}.{operation.name}'
        assert field.blank is True, f'{operation.model_name}.{operation.name}'
        assert field.editable is False, f'{operation.model_name}.{operation.name}'
        assert field.default is NOT_PROVIDED, f'{operation.model_name}.{operation.name}'
        assert field.db_index is False, f'{operation.model_name}.{operation.name}'

    assert listing_index_migration.atomic is False
    assert listing_index_migration.dependencies == [
        ('marketplaces', expand.name),
    ]
    assert len(listing_index_migration.operations) == 1
    listing_operation = listing_index_migration.operations[0]
    assert isinstance(listing_operation, AddIndexConcurrently)
    assert listing_operation.model_name == 'listing'
    assert listing_operation.index.name == LISTING_DUE_INDEX
    assert listing_operation.index.fields == [
        'account', 'status', 'next_status_check_at', 'id',
    ]
    assert listing_operation.index.condition == LISTING_DUE_CONDITION

    assert account_index_migration.atomic is False
    assert account_index_migration.dependencies == [
        ('marketplaces', listing_index_migration.name),
    ]
    assert len(account_index_migration.operations) == 1
    account_operation = account_index_migration.operations[0]
    assert isinstance(account_operation, AddIndexConcurrently)
    assert account_operation.model_name == 'marketplaceaccount'
    assert account_operation.index.name == ACCOUNT_DUE_INDEX
    assert account_operation.index.fields == [
        'marketplace', 'status_batch_due_at', 'id',
    ]
    assert account_operation.index.condition == ACCOUNT_DUE_CONDITION


@pytest.mark.django_db
def test_status_lifecycle_expand_columns_have_no_database_defaults():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL catalog contract')

    expected_by_table = {
        Listing._meta.db_table: set(LISTING_EXPAND_FIELDS),
        MarketplaceAccount._meta.db_table: set(ACCOUNT_EXPAND_FIELDS),
    }
    with connection.cursor() as cursor:
        for table_name, expected_columns in expected_by_table.items():
            cursor.execute(
                '''
                SELECT column_name, is_nullable, column_default
                  FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = %s
                   AND column_name = ANY(%s)
                ''',
                [table_name, list(expected_columns)],
            )
            columns = {
                name: (is_nullable, column_default)
                for name, is_nullable, column_default in cursor.fetchall()
            }
            assert set(columns) == expected_columns
            assert all(nullable == 'YES' for nullable, _ in columns.values())
            assert all(default is None for _, default in columns.values())

            cursor.execute(
                '''
                SELECT attribute.attname, attribute.atthasmissing
                  FROM pg_attribute AS attribute
                  JOIN pg_class AS table_class
                    ON table_class.oid = attribute.attrelid
                  JOIN pg_namespace AS namespace
                    ON namespace.oid = table_class.relnamespace
                 WHERE namespace.nspname = current_schema()
                   AND table_class.relname = %s
                   AND attribute.attname = ANY(%s)
                   AND attribute.attnum > 0
                   AND NOT attribute.attisdropped
                ''',
                [table_name, list(expected_columns)],
            )
            missing_flags = dict(cursor.fetchall())
            assert set(missing_flags) == expected_columns
            assert not any(missing_flags.values())


@pytest.mark.django_db
def test_listing_due_index_is_valid_postgresql_index():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL catalog contract')

    with connection.cursor() as cursor:
        cursor.execute(
            '''
            SELECT table_class.relname,
                   pg_index.indisvalid,
                   pg_index.indisready,
                   pg_index.indisunique,
                   pg_get_indexdef(pg_index.indexrelid),
                   pg_get_expr(pg_index.indpred, pg_index.indrelid)
              FROM pg_index
              JOIN pg_class AS index_class
                ON index_class.oid = pg_index.indexrelid
              JOIN pg_class AS table_class
                ON table_class.oid = pg_index.indrelid
              JOIN pg_namespace AS namespace
                ON namespace.oid = table_class.relnamespace
             WHERE namespace.nspname = current_schema()
               AND index_class.relname = %s
            ''',
            [LISTING_DUE_INDEX],
        )
        row = cursor.fetchone()

    assert row is not None
    table, valid, ready, unique, definition, predicate = row
    assert table == Listing._meta.db_table
    assert valid is True
    assert ready is True
    assert unique is False
    assert predicate is not None

    definition = ' '.join(definition.replace('"', '').split()).lower()
    predicate = ' '.join(predicate.replace('"', '').split()).lower()
    assert '(account_id, status, next_status_check_at, id)' in definition
    assert 'deleted_at is null' in predicate
    assert 'external_id is not null' in predicate
    assert 'next_status_check_at is not null' in predicate
    assert "external_id)::text = ''::text" in predicate
    assert 'not (' in predicate
    for canonical_status in (
        Listing.STATUS_PENDING,
        Listing.STATUS_ACTIVE,
        Listing.STATUS_ARCHIVING,
    ):
        assert f"'{canonical_status}'" not in predicate


@pytest.mark.django_db
def test_account_due_index_is_valid_postgresql_index():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL catalog contract')

    with connection.cursor() as cursor:
        cursor.execute(
            '''
            SELECT table_class.relname,
                   pg_index.indisvalid,
                   pg_index.indisready,
                   pg_index.indisunique,
                   pg_get_indexdef(pg_index.indexrelid),
                   pg_get_expr(pg_index.indpred, pg_index.indrelid)
              FROM pg_index
              JOIN pg_class AS index_class
                ON index_class.oid = pg_index.indexrelid
              JOIN pg_class AS table_class
                ON table_class.oid = pg_index.indrelid
              JOIN pg_namespace AS namespace
                ON namespace.oid = table_class.relnamespace
             WHERE namespace.nspname = current_schema()
               AND index_class.relname = %s
            ''',
            [ACCOUNT_DUE_INDEX],
        )
        row = cursor.fetchone()

    assert row is not None
    table, valid, ready, unique, definition, predicate = row
    assert table == MarketplaceAccount._meta.db_table
    assert valid is True
    assert ready is True
    assert unique is False
    assert predicate is not None

    definition = ' '.join(definition.replace('"', '').split()).lower()
    predicate = ' '.join(predicate.replace('"', '').split()).lower()
    assert '(marketplace, status_batch_due_at, id)' in definition
    assert 'deleted_at is null' in predicate
    assert 'is_active' in predicate
    assert 'status_batch_due_at is not null' in predicate


@pytest.mark.django_db
def test_models_can_be_created_without_status_lifecycle_values():
    tenant = Tenant.objects.create(name='Status expand', slug='status-expand')
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Expand account',
        external_id='status-expand-account',
        credentials_enc=b'opaque-test-credentials',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='STATUS-EXPAND-1',
        name='Status expand product',
        price=Decimal('1000.00'),
    )
    listing = Listing.objects.create(
        tenant=tenant,
        product=product,
        account=account,
        price_on_listing=Decimal('1100.00'),
    )

    for field_name in LISTING_EXPAND_FIELDS:
        assert getattr(listing, field_name) is None
    for field_name in ACCOUNT_EXPAND_FIELDS:
        assert getattr(account, field_name) is None


@pytest.mark.django_db(transaction=True)
def test_existing_marketplace_rows_survive_upgrade_from_0019():
    tenant = Tenant.objects.create(name='Upgrade tenant', slug='upgrade-tenant')
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        name='Upgrade account',
        external_id='upgrade-account',
        credentials_enc=b'opaque-upgrade-credentials',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='UPGRADE-1',
        name='Upgrade product',
        price=Decimal('1000.00'),
    )
    listing = Listing.objects.create(
        tenant=tenant,
        product=product,
        account=account,
        external_id='upgrade-listing',
        status=Listing.STATUS_ACTIVE,
        price_on_listing=Decimal('1100.00'),
    )
    account_pk = account.pk
    listing_pk = listing.pk

    executor = MigrationExecutor(connection)
    executor.migrate([('marketplaces', '0019_soft_delete_core_entities')])

    executor = MigrationExecutor(connection)
    executor.migrate([
        ('marketplaces', '0028_feed_run_source_intent_unique'),
    ])

    upgraded_account = MarketplaceAccount.objects.get(pk=account_pk)
    upgraded_listing = Listing.objects.get(pk=listing_pk)
    assert upgraded_account.external_id == 'upgrade-account'
    assert upgraded_listing.external_id == 'upgrade-listing'
    assert upgraded_listing.status == Listing.STATUS_ACTIVE
    for field_name in ACCOUNT_EXPAND_FIELDS:
        assert getattr(upgraded_account, field_name) is None
    for field_name in LISTING_EXPAND_FIELDS:
        assert getattr(upgraded_listing, field_name) is None
