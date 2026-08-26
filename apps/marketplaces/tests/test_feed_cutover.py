import base64
import io
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from apps.marketplaces.adapters.avito.feed_builder import build_stop_feed
from apps.marketplaces.feed_artifact_canary import (
    activate_private_feed_canary,
    rollback_private_feed_canary,
)
from apps.marketplaces.feed_artifact_serving import private_feed_route_enabled
from apps.marketplaces.feed_cutover import (
    private_feed_cutover_account_ids,
    private_feed_cutover_enabled,
)
from apps.marketplaces.feed_intents import bump_feed_intents
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import (
    Listing,
    MarketplaceAccount,
    MarketplaceFeedEndpoint,
    MarketplaceFeedRun,
)
from apps.marketplaces.tasks import (
    _coalesced_flush_durable,
    _durable_feed_run_enabled,
)
from apps.tenants.models import Tenant
from apps.products.models import Product


class _ExactVersionClient:
    put_total_max_attempts = 1

    def __init__(self):
        self.body = b''
        self.request = {}

    def put_object_once(self, **kwargs):
        self.request = kwargs.copy()
        self.body = kwargs['Body'].read()
        return {
            'VersionId': 'cutover-version-1',
            'ChecksumSHA256': kwargs['ChecksumSHA256'],
        }

    def _response(self, *, include_body=False):
        response = {
            'VersionId': 'cutover-version-1',
            'ContentLength': len(self.body),
            'ContentType': 'application/xml',
            'Metadata': self.request['Metadata'].copy(),
            'ChecksumSHA256': base64.b64encode(
                bytes.fromhex(self.request['Metadata']['payload-sha256']),
            ).decode('ascii'),
        }
        if include_body:
            response['Body'] = io.BytesIO(self.body)
        return response

    def head_object(self, **kwargs):
        return self._response()

    def get_object(self, **kwargs):
        return self._response(include_body=True)


class _UnknownPutClient(_ExactVersionClient):
    def put_object_once(self, **kwargs):
        self.request = kwargs.copy()
        self.body = kwargs['Body'].read()
        raise OSError('simulated lost PUT response')


def _active_settings(settings):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_RUN_MODE = 'legacy'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_ARTIFACT_MODE = 'active'
    settings.MARKETPLACE_FEED_STORAGE_MODE = 'stable_bridge'
    settings.MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED = False
    settings.MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS = (4,)


def _cutover_endpoint(settings, slug: str):
    tenant = Tenant.objects.create(name=f'Cutover {slug}', slug=slug)
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Cutover Avito {slug}',
        external_id=f'cutover-avito-{slug}',
        credentials_enc=b'opaque-cutover-credentials',
        feed_intent_revision=1,
        feed_intent_dispatched_revision=0,
        feed_intent_due_at=timezone.now(),
    )
    _active_settings(settings)
    settings.MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS = (account.pk,)
    settings.MARKETPLACE_FEED_ARTIFACT_BUCKET = 'private-feed-artifacts'
    settings.MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER = 'folder-123'
    settings.MARKETPLACE_FEED_ARTIFACT_MAX_BYTES = 1024 * 1024
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='feed-hmac-v1',
        owner_identity_digest=account_identity_digest(account),
        serve_enabled=True,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
        legacy_object_key=f'feeds/{slug}/feed.xml',
        legacy_profile_url=f'https://legacy.example.test/{slug}/feed.xml',
        profile_state=MarketplaceFeedEndpoint.ProfileState.VERIFIED,
        profile_fingerprint='a' * 64,
        profile_revision=1,
        profile_verified_at=timezone.now(),
        source_intent_revision=1,
    )
    return account, endpoint


def test_only_allowlisted_account_enters_private_cutover(settings):
    _active_settings(settings)

    assert private_feed_cutover_account_ids() == frozenset({4})
    assert private_feed_cutover_enabled(4) is True
    assert private_feed_cutover_enabled(5) is False
    assert _durable_feed_run_enabled(4) is True
    assert _durable_feed_run_enabled(5) is False


def test_cutover_fails_closed_when_any_coordinated_gate_changes(settings):
    _active_settings(settings)
    settings.MARKETPLACE_FEED_ARTIFACT_MODE = 'disabled'

    assert private_feed_cutover_enabled(4) is False
    assert _durable_feed_run_enabled(4) is False


def test_active_private_route_is_dark_for_non_allowlisted_account(settings):
    _active_settings(settings)
    admitted = SimpleNamespace(
        account_id=4,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        serve_enabled=True,
    )
    not_admitted = SimpleNamespace(
        account_id=5,
        storage_mode=MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION,
        serve_enabled=True,
    )

    assert private_feed_route_enabled(admitted) is True
    assert private_feed_route_enabled(not_admitted) is False


def test_durable_flush_routes_only_allowlisted_account_to_private_worker(settings):
    _active_settings(settings)
    task = object()
    account = type('Account', (), {'pk': 4})()
    expected = {'status': 'private'}

    with patch(
        'apps.marketplaces.tasks._coalesced_flush_private_durable',
        return_value=expected,
    ) as private_worker:
        assert _coalesced_flush_durable(task, account) == expected

    private_worker.assert_called_once_with(task, account)


@pytest.mark.django_db
def test_private_cutover_builds_verifies_promotes_then_submits_stop_feed(settings):
    account, endpoint = _cutover_endpoint(settings, 'cutover-success')
    task = SimpleNamespace(request=SimpleNamespace(retries=0), max_retries=5)
    client = _ExactVersionClient()

    with (
        patch(
            'apps.marketplaces.feed_artifact_clients.private_feed_object_client',
            return_value=client,
        ),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value={},
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter._trigger_autoload') as trigger,
        patch('apps.marketplaces.tasks._enqueue_feed_run_snapshot'),
    ):
        result = _coalesced_flush_durable(task, account)

    endpoint.refresh_from_db()
    run = MarketplaceFeedRun.objects.get(account=account)
    assert result == {'status': 'submitted', 'run_id': str(run.pk)}
    assert client.body == build_stop_feed()
    assert endpoint.storage_mode == (
        MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
    )
    assert endpoint.serve_enabled is True
    assert endpoint.current_artifact_id == run.feed_artifact_id
    assert run.state == MarketplaceFeedRun.State.POLLING
    assert run.source_intent_revision == 1
    trigger.assert_called_once_with()


@pytest.mark.django_db
def test_private_cutover_streams_current_nonempty_feed(settings):
    account, endpoint = _cutover_endpoint(settings, 'cutover-nonempty')
    product = Product.objects.create(
        tenant=account.tenant,
        article='CUTOVER-NONEMPTY-1',
        name='Cutover nonempty product',
        price=Decimal('1000.00'),
    )
    Listing.objects.create(
        tenant=account.tenant,
        account=account,
        product=product,
        status=Listing.STATUS_PENDING,
        price_on_listing=Decimal('1100.00'),
    )
    task = SimpleNamespace(request=SimpleNamespace(retries=0), max_retries=5)
    client = _ExactVersionClient()

    with (
        patch(
            'apps.marketplaces.feed_artifact_clients.private_feed_object_client',
            return_value=client,
        ),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value={},
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter._trigger_autoload'),
        patch('apps.marketplaces.tasks._enqueue_feed_run_snapshot'),
    ):
        result = _coalesced_flush_durable(task, account)

    endpoint.refresh_from_db()
    run = MarketplaceFeedRun.objects.get(account=account)
    assert result == {'status': 'submitted', 'run_id': str(run.pk)}
    assert b'<Ad>' in client.body
    assert b'CUTOVER-NONEMPTY-1' in client.body
    assert endpoint.current_artifact_id == run.feed_artifact_id


@pytest.mark.django_db
def test_private_cutover_never_repeats_unknown_put_and_keeps_legacy_serving(
    settings,
):
    account, endpoint = _cutover_endpoint(settings, 'cutover-put-unknown')
    task = SimpleNamespace(request=SimpleNamespace(retries=0), max_retries=5)
    client = _UnknownPutClient()

    with (
        patch(
            'apps.marketplaces.feed_artifact_clients.private_feed_object_client',
            return_value=client,
        ),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
        ) as latest_upload,
        patch('apps.marketplaces.tasks.AvitoAdapter._trigger_autoload') as trigger,
    ):
        result = _coalesced_flush_durable(task, account)

    endpoint.refresh_from_db()
    account.refresh_from_db()
    run = MarketplaceFeedRun.objects.get(account=account)
    assert result['status'] == 'private_artifact_put_unknown'
    assert result['run_id'] == str(run.pk)
    assert endpoint.storage_mode == (
        MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
    )
    assert endpoint.current_artifact_id is None
    assert account.feed_intent_due_at is None
    assert run.state == MarketplaceFeedRun.State.PREPARING
    assert client.body == build_stop_feed()
    latest_upload.assert_not_called()
    trigger.assert_not_called()


@pytest.mark.django_db
def test_private_cutover_replaces_prior_canary_artifact_atomically(settings):
    account, endpoint = _cutover_endpoint(settings, 'cutover-successor')
    MarketplaceAccount.objects.filter(pk=account.pk).update(
        feed_intent_dispatched_revision=1,
        feed_intent_due_at=None,
    )
    settings.MARKETPLACE_FEED_ARTIFACT_MODE = 'canary'
    settings.MARKETPLACE_FEED_STORAGE_MODE = 'private_generation'
    settings.MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS = ()
    first_client = _ExactVersionClient()
    with (
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_bucket_preflight',
            return_value={'versioning': 'Enabled'},
        ),
        patch(
            'apps.marketplaces.feed_artifact_canary.private_feed_object_client',
            return_value=first_client,
        ),
    ):
        canary = activate_private_feed_canary(account.pk)
    rollback_private_feed_canary(
        account.pk,
        expected_artifact_id=canary.artifact_id,
        expected_artifact_revision=canary.artifact_revision,
    )

    with transaction.atomic():
        bump_feed_intents([account.pk], timezone.now())
    account.refresh_from_db()
    _active_settings(settings)
    settings.MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS = (account.pk,)
    settings.MARKETPLACE_FEED_ARTIFACT_BUCKET = 'private-feed-artifacts'
    settings.MARKETPLACE_FEED_ARTIFACT_EXPECTED_BUCKET_OWNER = 'folder-123'
    settings.MARKETPLACE_FEED_ARTIFACT_MAX_BYTES = 1024 * 1024
    second_client = _ExactVersionClient()
    task = SimpleNamespace(request=SimpleNamespace(retries=0), max_retries=5)

    with (
        patch(
            'apps.marketplaces.feed_artifact_clients.private_feed_object_client',
            return_value=second_client,
        ),
        patch(
            'apps.marketplaces.tasks.AvitoAdapter.get_latest_upload',
            return_value={},
        ),
        patch('apps.marketplaces.tasks.AvitoAdapter._trigger_autoload'),
        patch('apps.marketplaces.tasks._enqueue_feed_run_snapshot'),
    ):
        result = _coalesced_flush_durable(task, account)

    endpoint.refresh_from_db()
    successor = MarketplaceFeedRun.objects.get(
        account=account,
        source_intent_revision=2,
    )
    assert result == {'status': 'submitted', 'run_id': str(successor.pk)}
    assert successor.predecessor_artifact_id == canary.artifact_id
    assert successor.feed_artifact_id != canary.artifact_id
    assert endpoint.current_artifact_id == successor.feed_artifact_id
    assert endpoint.artifact_revision == canary.artifact_revision + 1
    assert endpoint.storage_mode == (
        MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
    )


@pytest.mark.django_db
def test_activation_command_arms_one_exact_ready_account(settings):
    account, _endpoint = _cutover_endpoint(settings, 'cutover-command')
    MarketplaceAccount.objects.filter(pk=account.pk).update(
        feed_intent_dispatched_revision=1,
        feed_intent_due_at=None,
    )
    stdout = io.StringIO()

    with (
        patch(
            'apps.marketplaces.feed_artifact_clients.'
            'private_feed_bucket_preflight',
            return_value={'versioning': 'Enabled'},
        ) as preflight,
        patch('apps.marketplaces.tasks.request_feed_flush') as request_flush,
    ):
        call_command(
            'activate_marketplace_feed_cutover',
            '--account-id',
            str(account.pk),
            '--confirm-account-id',
            str(account.pk),
            '--apply',
            stdout=stdout,
        )

    account.refresh_from_db()
    output = json.loads(stdout.getvalue())
    assert output == {
        'account_id': account.pk,
        'ok': True,
        'source_intent_revision': 2,
        'status': 'armed',
    }
    assert account.feed_intent_revision == 2
    assert account.feed_intent_due_at is not None
    preflight.assert_called_once_with()
    request_flush.assert_called_once()
    assert request_flush.call_args.args[0].pk == account.pk
