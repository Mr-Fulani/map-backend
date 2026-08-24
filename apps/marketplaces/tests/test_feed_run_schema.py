from decimal import Decimal

import pytest
from django.contrib import admin
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import IntegrityError, connection, migrations, models, transaction
from django.db.migrations.loader import MigrationLoader
from django.db.models.fields import NOT_PROVIDED

from apps.marketplaces.models import (
    Listing,
    MarketplaceAccount,
    MarketplaceFeedRun,
)
from apps.products.models import Product
from apps.tenants.models import Tenant


ACTIVE_STATES = (
    MarketplaceFeedRun.State.PREPARING,
    MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
    MarketplaceFeedRun.State.POLLING,
    MarketplaceFeedRun.State.REPORTING,
    MarketplaceFeedRun.State.RETRY_WAIT,
)
TERMINAL_STATES = (
    MarketplaceFeedRun.State.SUCCEEDED,
    MarketplaceFeedRun.State.FAILED,
    MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
    MarketplaceFeedRun.State.SUPERSEDED,
    MarketplaceFeedRun.State.CANCELLED,
)
OWNERSHIP_STATES = (
    *ACTIVE_STATES,
    MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
)
ACTIVE_CONDITION = models.Q(state__in=ACTIVE_STATES)
OWNERSHIP_CONDITION = models.Q(state__in=OWNERSHIP_STATES)
DUE_CONDITION = models.Q(
    state__in=ACTIVE_STATES,
    next_attempt_at__isnull=False,
)
PROVIDER_REF_CONDITION = (
    models.Q(provider_run_id__isnull=False)
    & ~models.Q(provider_run_id='')
)
LISTING_PENDING_CONDITION = models.Q(
    deleted_at__isnull=True,
    external_id__isnull=True,
    feed_run__isnull=False,
)


def _index(model, name):
    matches = [index for index in model._meta.indexes if index.name == name]
    assert len(matches) == 1
    return matches[0]


def _constraint(model, name):
    matches = [constraint for constraint in model._meta.constraints if constraint.name == name]
    assert len(matches) == 1
    return matches[0]


def _account(*, slug='feed-schema'):
    tenant = Tenant.objects.create(name='Feed schema', slug=slug)
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name='Feed account',
        external_id=f'{slug}-account',
        credentials_enc=b'opaque-test-credentials',
    )
    return tenant, account


def _run(*, tenant, account, **values):
    values.setdefault('marketplace', account.marketplace)
    values.setdefault('account_identity_digest', 'a' * 64)
    return MarketplaceFeedRun.objects.create(
        tenant=tenant,
        account=account,
        **values,
    )


def test_feed_run_model_contract_is_provider_neutral_and_fenced():
    assert MarketplaceFeedRun.ACTIVE_STATES == tuple(ACTIVE_STATES)
    assert MarketplaceFeedRun.OWNERSHIP_STATES == tuple(OWNERSHIP_STATES)
    assert MarketplaceFeedRun.TERMINAL_STATES == tuple(TERMINAL_STATES)
    assert set(MarketplaceFeedRun.ACTIVE_STATES).isdisjoint(
        MarketplaceFeedRun.TERMINAL_STATES,
    )
    assert tuple(value for value, _label in MarketplaceFeedRun.State.choices) == (
        *ACTIVE_STATES,
        *TERMINAL_STATES,
    )

    generation = MarketplaceFeedRun._meta.pk
    assert isinstance(generation, models.UUIDField)
    assert generation.name == 'id'
    assert generation.editable is False

    assert MarketplaceFeedRun._meta.get_field('tenant').remote_field.on_delete is models.CASCADE
    account = MarketplaceFeedRun._meta.get_field('account')
    assert account.remote_field.on_delete is models.CASCADE
    assert account.remote_field.related_name == 'feed_runs'

    state = MarketplaceFeedRun._meta.get_field('state')
    assert state.default == MarketplaceFeedRun.State.PREPARING
    assert state.editable is False
    assert state.max_length == 20

    for name, expected_default in (
        ('revision', 0),
        ('poll_cursor_listing_id', 0),
        ('poll_round', 0),
        ('report_page', 1),
        ('report_attempt', 0),
        ('submission_reconcile_attempt', 0),
        ('total_count', 0),
        ('published_count', 0),
        ('rejected_count', 0),
        ('pending_count', 0),
    ):
        field = MarketplaceFeedRun._meta.get_field(name)
        assert field.default == expected_default, name
        assert field.editable is False, name

    assert MarketplaceFeedRun._meta.get_field('account_identity_digest').max_length == 64
    payload = MarketplaceFeedRun._meta.get_field('payload_sha256')
    assert payload.max_length == 64
    assert payload.blank is True
    assert payload.default is NOT_PROVIDED

    for name in ('provider_run_id', 'provider_predecessor_run_id'):
        provider_run = MarketplaceFeedRun._meta.get_field(name)
        assert provider_run.max_length == 200
        assert provider_run.null is True
        assert provider_run.blank is True
        assert provider_run.editable is False
        assert provider_run.default is NOT_PROVIDED

    for name in (
        'submitted_at', 'provider_result_deadline_at', 'report_completed_at',
        'next_attempt_at', 'claim_token', 'claimed_until', 'finished_at',
    ):
        field = MarketplaceFeedRun._meta.get_field(name)
        assert field.null is True, name
        assert field.blank is True, name
        assert field.editable is False, name
        assert field.default is NOT_PROVIDED, name

    last_error = MarketplaceFeedRun._meta.get_field('last_error')
    assert isinstance(last_error, models.TextField)
    assert last_error.max_length == 2000
    assert last_error.blank is True
    assert last_error.editable is False


def test_feed_run_constraints_and_indexes_match_scheduler_queries():
    owner = _constraint(MarketplaceFeedRun, 'uniq_mkt_feed_owner_account')
    assert isinstance(owner, models.UniqueConstraint)
    assert owner.fields == ('account',)
    assert owner.condition == OWNERSHIP_CONDITION

    provider_ref = _constraint(MarketplaceFeedRun, 'uniq_mkt_feed_provider_ref')
    assert isinstance(provider_ref, models.UniqueConstraint)
    assert provider_ref.fields == ('account', 'provider_run_id')
    assert provider_ref.condition == PROVIDER_REF_CONDITION

    due = _index(MarketplaceFeedRun, 'mkt_feed_due_idx')
    assert due.fields == ['marketplace', 'next_attempt_at', 'id']
    assert due.condition == DUE_CONDITION

    pending = _index(Listing, 'mkt_lst_feed_pending')
    assert pending.fields == ['feed_run', 'status', 'id']
    assert pending.condition == LISTING_PENDING_CONDITION

    for named_object in (*MarketplaceFeedRun._meta.constraints, *MarketplaceFeedRun._meta.indexes, pending):
        assert len(named_object.name) <= 30
        predicate = getattr(named_object, 'condition', None)
        assert predicate is not None
        # PostgreSQL partial-index predicates must be immutable.  Match the
        # SQL function, not the valid state name ``submit_unknown``.
        assert 'now(' not in str(predicate).lower().replace(' ', '')


def test_listing_feed_run_field_is_nullable_set_null_and_not_editable():
    field = Listing._meta.get_field('feed_run')
    assert field.null is True
    assert field.blank is True
    assert field.db_index is False
    assert field.editable is False
    assert field.default is NOT_PROVIDED
    assert field.remote_field.on_delete is models.SET_NULL
    assert field.remote_field.related_name == 'listings'


@pytest.mark.django_db
def test_feed_run_database_enforces_one_active_generation_per_account():
    tenant, account = _account(slug='feed-active-constraint')
    first = _run(tenant=tenant, account=account)

    with pytest.raises(IntegrityError), transaction.atomic():
        _run(
            tenant=tenant,
            account=account,
            state=MarketplaceFeedRun.State.POLLING,
        )

    first.state = MarketplaceFeedRun.State.SUCCEEDED
    first.save(update_fields=['state', 'updated_at'])
    second = _run(
        tenant=tenant,
        account=account,
        state=MarketplaceFeedRun.State.POLLING,
    )
    assert second.pk != first.pk


@pytest.mark.django_db
def test_uncertain_generation_retains_exclusive_account_ownership():
    tenant, account = _account(slug='feed-uncertain-owner-constraint')
    _run(
        tenant=tenant,
        account=account,
        state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        _run(tenant=tenant, account=account)


@pytest.mark.django_db
def test_feed_run_provider_reference_is_unique_per_account_when_present():
    tenant, account = _account(slug='feed-provider-constraint')
    _run(
        tenant=tenant,
        account=account,
        state=MarketplaceFeedRun.State.SUCCEEDED,
        provider_run_id='provider-upload-1',
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        _run(
            tenant=tenant,
            account=account,
            state=MarketplaceFeedRun.State.FAILED,
            provider_run_id='provider-upload-1',
        )

    # Missing provider identity is deliberately not globally unique.
    _run(
        tenant=tenant,
        account=account,
        state=MarketplaceFeedRun.State.SUCCEEDED,
    )
    _run(
        tenant=tenant,
        account=account,
        state=MarketplaceFeedRun.State.FAILED,
    )


@pytest.mark.django_db
def test_legacy_listing_create_remains_valid_without_feed_generation():
    tenant, account = _account(slug='feed-legacy-create')
    product = Product.objects.create(
        tenant=tenant,
        article='FEED-LEGACY-1',
        name='Legacy feed product',
        price=Decimal('1000.00'),
    )

    listing = Listing.objects.create(
        tenant=tenant,
        product=product,
        account=account,
        price_on_listing=Decimal('1100.00'),
    )

    assert listing.feed_run_id is None


@pytest.mark.django_db
def test_feed_run_migration_is_additive_and_has_no_backfill():
    loader = MigrationLoader(connection)
    matches = [
        migration
        for (app_label, name), migration in loader.disk_migrations.items()
        if app_label == 'marketplaces' and name.startswith('0023_')
    ]
    assert len(matches) == 1
    migration = matches[0]
    assert migration.dependencies == [
        ('marketplaces', '0022_account_status_lifecycle_concurrent_index'),
    ]
    assert [type(operation) for operation in migration.operations] == [
        migrations.CreateModel,
        migrations.AddField,
    ]

    create, add_field = migration.operations
    assert create.name == 'MarketplaceFeedRun'
    create_fields = dict(create.fields)
    assert {
        'provider_predecessor_run_id',
        'provider_result_deadline_at',
        'report_completed_at',
    }.issubset(create_fields)
    assert create_fields['provider_predecessor_run_id'].max_length == 200
    for name in (
        'provider_predecessor_run_id',
        'provider_result_deadline_at',
        'report_completed_at',
    ):
        assert create_fields[name].null is True
        assert create_fields[name].default is NOT_PROVIDED
    assert add_field.model_name == 'listing'
    assert add_field.name == 'feed_run'
    assert add_field.field.null is True
    assert add_field.field.db_index is False
    assert add_field.field.default is NOT_PROVIDED
    owner_constraints = [
        constraint
        for constraint in create.options['constraints']
        if constraint.name == 'uniq_mkt_feed_owner_account'
    ]
    assert len(owner_constraints) == 1
    assert owner_constraints[0].condition == OWNERSHIP_CONDITION
    assert not any(
        isinstance(operation, (migrations.RunPython, migrations.RunSQL))
        for operation in migration.operations
    )


@pytest.mark.django_db
def test_listing_feed_run_index_is_built_concurrently_after_expand():
    loader = MigrationLoader(connection)
    matches = [
        migration
        for (app_label, name), migration in loader.disk_migrations.items()
        if app_label == 'marketplaces' and name.startswith('0024_')
    ]
    assert len(matches) == 1
    migration = matches[0]
    assert migration.atomic is False
    assert migration.dependencies == [
        ('marketplaces', '0023_marketplace_feed_run'),
    ]
    assert len(migration.operations) == 1
    operation = migration.operations[0]
    assert isinstance(operation, AddIndexConcurrently)
    assert operation.model_name == 'listing'
    assert operation.index.name == 'mkt_lst_feed_pending'
    assert operation.index.fields == ['feed_run', 'status', 'id']
    assert operation.index.condition == LISTING_PENDING_CONDITION


@pytest.mark.django_db
def test_feed_run_partial_indexes_are_valid_in_postgresql_catalog():
    if connection.vendor != 'postgresql':
        pytest.skip('PostgreSQL catalog contract')

    expected_names = ['mkt_feed_due_idx', 'mkt_lst_feed_pending']
    with connection.cursor() as cursor:
        cursor.execute(
            '''
            SELECT index_class.relname,
                   table_class.relname,
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
               AND index_class.relname = ANY(%s)
            ''',
            [expected_names],
        )
        found = {
            name: (table, valid, ready, unique, definition, predicate)
            for name, table, valid, ready, unique, definition, predicate
            in cursor.fetchall()
        }

    assert set(found) == set(expected_names)
    for _table, valid, ready, unique, _definition, predicate in found.values():
        assert valid is True
        assert ready is True
        assert unique is False
        assert predicate is not None

    assert found['mkt_feed_due_idx'][0] == MarketplaceFeedRun._meta.db_table
    assert found['mkt_lst_feed_pending'][0] == Listing._meta.db_table
    listing_predicate = ' '.join(
        found['mkt_lst_feed_pending'][5].replace('"', '').split()
    ).lower()
    assert 'deleted_at is null' in listing_predicate
    assert 'external_id is null' in listing_predicate
    assert 'feed_run_id is not null' in listing_predicate

    with connection.cursor() as cursor:
        cursor.execute(
            '''
            SELECT index_class.relname,
                   pg_get_expr(pg_index.indpred, pg_index.indrelid)
              FROM pg_index
              JOIN pg_class AS index_class
                ON index_class.oid = pg_index.indexrelid
              JOIN pg_class AS table_class
                ON table_class.oid = pg_index.indrelid
              JOIN pg_namespace AS namespace
                ON namespace.oid = table_class.relnamespace
              JOIN pg_attribute AS attribute
                ON attribute.attrelid = table_class.oid
               AND attribute.attname = 'feed_run_id'
               AND attribute.attnum = ANY(pg_index.indkey)
             WHERE namespace.nspname = current_schema()
               AND table_class.relname = %s
            ''',
            [Listing._meta.db_table],
        )
        feed_run_indexes = cursor.fetchall()

    assert feed_run_indexes == [('mkt_lst_feed_pending', found['mkt_lst_feed_pending'][5])]


def test_feed_run_admin_is_registered_read_only():
    model_admin = admin.site._registry[MarketplaceFeedRun]

    assert model_admin.has_add_permission(None) is False
    assert model_admin.has_change_permission(None) is False
    assert model_admin.has_delete_permission(None) is False
    assert set(model_admin.readonly_fields) == {
        field.name for field in MarketplaceFeedRun._meta.fields
    }
