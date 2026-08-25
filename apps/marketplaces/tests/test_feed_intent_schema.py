import importlib

import pytest
from django.db import IntegrityError, transaction
from django.db import models

from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedEndpoint,
    MarketplaceFeedRun,
)
from apps.tenants.models import Tenant


def _account() -> MarketplaceAccount:
    tenant = Tenant.objects.create(name='Intent schema tenant', slug='intent-schema')
    return MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name='Intent schema account',
        external_id='intent-schema-account',
        credentials_enc=b'encrypted',
    )


def _named(model, collection: str, name: str):
    return next(
        item for item in getattr(model._meta, collection)
        if item.name == name
    )


def test_feed_intent_model_state_is_additive_and_legacy_safe():
    assert MarketplaceAccount._meta.get_field('feed_intent_revision').default == 0
    assert (
        MarketplaceAccount._meta.get_field(
            'feed_intent_dispatched_revision',
        ).default
        == 0
    )
    due_at = MarketplaceAccount._meta.get_field('feed_intent_due_at')
    assert due_at.null is True
    assert due_at.editable is False

    order = _named(MarketplaceAccount, 'constraints', 'mkt_acct_intent_order')
    assert order.condition == models.Q(
        feed_intent_dispatched_revision__lte=models.F('feed_intent_revision'),
    )
    due = _named(MarketplaceAccount, 'indexes', 'mkt_acct_feed_intent_due')
    assert due.fields == ['marketplace', 'feed_intent_due_at', 'id']
    assert due.condition == models.Q(
        deleted_at__isnull=True,
        is_active=True,
        feed_intent_due_at__isnull=False,
    )

    endpoint_source = MarketplaceFeedEndpoint._meta.get_field(
        'source_intent_revision',
    )
    assert endpoint_source.default == 0
    assert endpoint_source.editable is False

    run_source = MarketplaceFeedRun._meta.get_field('source_intent_revision')
    assert run_source.null is True
    assert run_source.editable is False
    source_unique = _named(
        MarketplaceFeedRun,
        'constraints',
        'uniq_mkt_feed_source_intent',
    )
    assert source_unique.fields == ('account', 'source_intent_revision')
    assert source_unique.condition == models.Q(source_intent_revision__isnull=False)


@pytest.mark.django_db(transaction=True)
def test_feed_intent_database_rejects_dispatched_cursor_ahead_of_desired():
    account = _account()

    with pytest.raises(IntegrityError), transaction.atomic():
        MarketplaceAccount.objects.filter(pk=account.pk).update(
            feed_intent_revision=1,
            feed_intent_dispatched_revision=2,
        )


def test_feed_intent_migration_chain_contains_no_private_artifact_schema():
    expand = importlib.import_module(
        'apps.marketplaces.migrations.0026_feed_intent_expand',
    ).Migration
    due = importlib.import_module(
        'apps.marketplaces.migrations.0027_feed_intent_due_concurrent_index',
    ).Migration
    source = importlib.import_module(
        'apps.marketplaces.migrations.0028_feed_run_source_intent_unique',
    ).Migration

    assert expand.dependencies == [
        ('marketplaces', '0025_marketplace_feed_endpoint'),
    ]
    assert due.dependencies == [('marketplaces', '0026_feed_intent_expand')]
    assert source.dependencies == [
        ('marketplaces', '0027_feed_intent_due_concurrent_index'),
    ]
    created_models = {
        operation.name
        for operation in expand.operations
        if operation.__class__.__name__ == 'CreateModel'
    }
    assert created_models == set()
    assert {
        operation.name
        for operation in expand.operations
        if operation.__class__.__name__ == 'AddField'
    } == {
        'feed_intent_revision',
        'feed_intent_dispatched_revision',
        'feed_intent_due_at',
        'source_intent_revision',
    }
