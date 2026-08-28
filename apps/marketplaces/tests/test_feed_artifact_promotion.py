import hashlib
import io
import tempfile
from dataclasses import dataclass, replace
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import transaction
from django.utils import timezone

from apps.marketplaces.feed_artifact_promotion import (
    FeedArtifactPromotionConfigurationError,
    StaleFeedArtifactPromotion,
    persist_private_feed_promotion_boundary,
)
from apps.marketplaces.feed_artifact_storage import (
    FeedArtifactResumeRequired,
    PrivateFeedArtifactStorageService,
)
from apps.marketplaces.feed_intents import bump_feed_intents
from apps.marketplaces.feed_workflow import (
    FeedRunClaim,
    account_identity_digest,
    claim_due_run_for_account,
)
from apps.marketplaces.models import (
    MarketplaceAccount,
    MarketplaceFeedArtifact,
    MarketplaceFeedArtifactUploadAttempt,
    MarketplaceFeedEndpoint,
    MarketplaceFeedRun,
)
from apps.tenants.models import Tenant


pytestmark = pytest.mark.django_db

_BUCKET = 'private-feed-artifacts'
_PROFILE_FINGERPRINT = 'a' * 64


class _ExactVersionClient:
    put_total_max_attempts = 1

    def __init__(self, *, after_put=None):
        self.body = b''
        self.metadata = {}
        self.content_type = ''
        self.checksum = ''
        self.after_put = after_put
        self.put_calls = 0

    def put_object_once(self, **kwargs):
        self.put_calls += 1
        self.body = kwargs['Body'].read()
        self.metadata = kwargs['Metadata'].copy()
        self.content_type = kwargs['ContentType']
        self.checksum = kwargs['ChecksumSHA256']
        response = {
            'VersionId': 'promotion-version-1',
            'ChecksumSHA256': self.checksum,
        }
        if self.after_put is not None:
            self.after_put()
        return response

    def _response(self, *, with_body=False):
        response = {
            'VersionId': 'promotion-version-1',
            'ChecksumSHA256': self.checksum,
            'ContentLength': len(self.body),
            'ContentType': self.content_type,
            'Metadata': self.metadata.copy(),
        }
        if with_body:
            response['Body'] = io.BytesIO(self.body)
        return response

    def head_object(self, **kwargs):
        return self._response()

    def get_object(self, **kwargs):
        return self._response(with_body=True)


@dataclass(frozen=True)
class _PromotionContext:
    account: MarketplaceAccount
    endpoint: MarketplaceFeedEndpoint
    run: MarketplaceFeedRun
    claim: FeedRunClaim
    artifact: MarketplaceFeedArtifact


@pytest.fixture(autouse=True)
def _artifact_settings(settings):
    settings.MARKETPLACE_FEED_RUN_MODE = 'durable'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'durable'
    # A real canary runtime must be able to create and promote consecutive
    # generations without mutating process settings between the two steps.
    settings.MARKETPLACE_FEED_ARTIFACT_MODE = 'canary'
    settings.MARKETPLACE_FEED_STORAGE_MODE = 'private_generation'
    settings.MARKETPLACE_FEED_ARTIFACT_BUCKET = _BUCKET
    settings.MARKETPLACE_FEED_ARTIFACT_MAX_BYTES = 1024 * 1024


def _enable_promotion(settings):
    settings.MARKETPLACE_FEED_ARTIFACT_MODE = 'canary'


def _enable_account_cutover(settings, account_id: int):
    settings.AVITO_STATUS_LIFECYCLE_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_RUN_MODE = 'legacy'
    settings.MARKETPLACE_FEED_INGRESS_MODE = 'dual_write'
    settings.MARKETPLACE_FEED_ARTIFACT_MODE = 'active'
    settings.MARKETPLACE_FEED_STORAGE_MODE = 'stable_bridge'
    settings.MARKETPLACE_FEED_PROFILE_MIGRATION_ENABLED = False
    settings.MARKETPLACE_FEED_CUTOVER_ACCOUNT_IDS = (account_id,)


def _attached_generation(
    slug: str,
    *,
    renew_during_put: bool = False,
    active_settings=None,
) -> _PromotionContext:
    payload = b'<Ads formatVersion="3" target="Avito.ru"></Ads>'
    created_at = timezone.now()
    tenant = Tenant.objects.create(name=f'Promotion {slug}', slug=slug)
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name=f'Promotion {slug}',
        external_id=f'{slug}-external',
        credentials_enc=b'opaque-promotion-credentials',
        feed_intent_revision=1,
        feed_intent_dispatched_revision=1,
    )
    if active_settings is not None:
        _enable_account_cutover(active_settings, account.pk)
    owner_digest = account_identity_digest(account)
    endpoint = MarketplaceFeedEndpoint.objects.create(
        account=account,
        token_key_id='promotion-hmac-v1',
        owner_identity_digest=owner_digest,
        storage_mode=(
            MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE
            if active_settings is not None
            else MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
        ),
        serve_enabled=active_settings is not None,
        legacy_object_key=f'feeds/{slug}/feed.xml',
        legacy_profile_url=f'https://legacy.example.test/{slug}/feed.xml',
        profile_state=MarketplaceFeedEndpoint.ProfileState.VERIFIED,
        profile_fingerprint=_PROFILE_FINGERPRINT,
        profile_revision=7,
        profile_verified_at=created_at,
        source_intent_revision=1,
    )
    run = MarketplaceFeedRun.objects.create(
        tenant=tenant,
        account=account,
        marketplace=account.marketplace,
        account_identity_digest=owner_digest,
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        state=MarketplaceFeedRun.State.PREPARING,
        next_attempt_at=created_at - timedelta(seconds=1),
        total_count=1,
        pending_count=1,
        source_intent_revision=1,
        endpoint_revision=0,
        predecessor_artifact_id=None,
        artifact_upload_attempt=0,
    )
    claim = claim_due_run_for_account(
        account.pk,
        expected_generation_id=run.pk,
        expected_revision=run.revision,
        now=created_at,
    )
    assert claim is not None

    def expire_lease():
        MarketplaceFeedRun.objects.filter(pk=run.pk).update(
            claimed_until=timezone.now() - timedelta(seconds=1),
            updated_at=timezone.now(),
        )

    client = _ExactVersionClient(
        after_put=expire_lease if renew_during_put else None,
    )
    service = PrivateFeedArtifactStorageService(
        client=client,
        bucket=_BUCKET,
        expected_bucket_owner='private-artifact-owner-1',
        max_bytes=1024 * 1024,
    )

    def upload(current_claim):
        with tempfile.TemporaryFile(mode='w+b') as payload_file:
            payload_file.write(payload)
            payload_file.flush()
            return service.upload_and_attach(
                current_claim,
                payload_file=payload_file,
                projection_count=1,
            )

    if renew_during_put:
        with pytest.raises(FeedArtifactResumeRequired):
            upload(claim)
        run.refresh_from_db()
        claim = claim_due_run_for_account(
            account.pk,
            expected_generation_id=run.pk,
            expected_revision=run.revision,
            now=timezone.now(),
        )
        assert claim is not None
    artifact = upload(claim)
    assert client.put_calls == 1
    run.refresh_from_db()
    return _PromotionContext(
        account=account,
        endpoint=endpoint,
        run=run,
        claim=claim,
        artifact=artifact,
    )


def _promote(context: _PromotionContext, *, now=None):
    transition_at = now or timezone.now()
    return persist_private_feed_promotion_boundary(
        context.claim,
        artifact_id=context.artifact.pk,
        expected_profile_revision=context.endpoint.profile_revision,
        expected_profile_fingerprint=context.endpoint.profile_fingerprint,
        provider_predecessor_run_id='provider-upload-before',
        submitted_at=transition_at,
        now=transition_at,
    )


def test_attached_exact_version_and_submission_boundary_commit_atomically(settings):
    context = _attached_generation('promotion-success')
    expected_profile_revision = context.endpoint.profile_revision
    expected_profile_fingerprint = context.endpoint.profile_fingerprint
    _enable_promotion(settings)
    transition_at = timezone.now()

    boundary = persist_private_feed_promotion_boundary(
        context.claim,
        artifact_id=context.artifact.pk,
        expected_profile_revision=expected_profile_revision,
        expected_profile_fingerprint=expected_profile_fingerprint,
        provider_predecessor_run_id='provider-upload-before',
        submitted_at=transition_at,
        now=transition_at,
    )

    context.endpoint.refresh_from_db()
    context.run.refresh_from_db()
    assert context.endpoint.current_artifact_id == context.artifact.pk
    assert context.endpoint.artifact_revision == 1
    assert context.endpoint.artifact_promoted_at == transition_at
    assert context.endpoint.serve_enabled is False
    assert context.run.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN
    assert context.run.provider_predecessor_run_id == 'provider-upload-before'
    assert context.run.submitted_at == transition_at
    assert context.run.revision == context.claim.revision + 1
    assert boundary.run_id == context.run.pk
    assert boundary.revision == context.run.revision


def test_active_account_cutover_promotes_legacy_bridge_without_global_durable(
    settings,
):
    context = _attached_generation(
        'active-account-cutover',
        active_settings=settings,
    )

    boundary = _promote(context)

    context.endpoint.refresh_from_db()
    context.run.refresh_from_db()
    assert context.endpoint.storage_mode == (
        MarketplaceFeedEndpoint.StorageMode.PRIVATE_GENERATION
    )
    assert context.endpoint.serve_enabled is True
    assert context.endpoint.current_artifact_id == context.artifact.pk
    assert context.endpoint.artifact_revision == 1
    assert context.run.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN
    assert boundary.revision == context.run.revision


def test_renewed_claim_can_attach_and_promote_original_put_revision(settings):
    context = _attached_generation(
        'promotion-renewed-claim',
        renew_during_put=True,
    )
    attempt = MarketplaceFeedArtifactUploadAttempt.objects.get(
        run=context.run,
    )
    assert attempt.put_run_revision < context.claim.revision
    assert attempt.state == MarketplaceFeedArtifactUploadAttempt.State.ATTACHED

    boundary = _promote(context)

    context.run.refresh_from_db()
    context.endpoint.refresh_from_db()
    assert context.endpoint.current_artifact_id == context.artifact.pk
    assert context.run.state == MarketplaceFeedRun.State.SUBMIT_UNKNOWN
    assert boundary.revision == context.run.revision


def test_promotion_boundary_rejects_nested_transaction_before_mutation(settings):
    context = _attached_generation('promotion-nested-transaction')

    with transaction.atomic():
        with pytest.raises(
            RuntimeError,
            match='durable atomic block cannot be nested',
        ):
            _promote(context)

    context.endpoint.refresh_from_db()
    context.run.refresh_from_db()
    assert context.endpoint.current_artifact_id is None
    assert context.endpoint.artifact_revision == 0
    assert context.endpoint.artifact_promoted_at is None
    assert context.run.state == MarketplaceFeedRun.State.PREPARING
    assert context.run.submitted_at is None
    assert context.run.provider_predecessor_run_id is None


@pytest.mark.parametrize('boundary_failure', ('none', 'exception'))
def test_boundary_failure_rolls_back_promoted_pointer(
    settings,
    boundary_failure,
):
    context = _attached_generation(f'promotion-boundary-{boundary_failure}')
    _enable_promotion(settings)
    if boundary_failure == 'none':
        patched_result = {'return_value': None}
        expected_exception = StaleFeedArtifactPromotion
    else:
        patched_result = {'side_effect': RuntimeError('boundary failed')}
        expected_exception = RuntimeError

    with (
        patch(
            'apps.marketplaces.feed_artifact_promotion.'
            'persist_feed_submission_boundary',
            **patched_result,
        ),
        pytest.raises(expected_exception),
    ):
        _promote(context)

    context.endpoint.refresh_from_db()
    context.run.refresh_from_db()
    assert context.endpoint.current_artifact_id is None
    assert context.endpoint.artifact_revision == 0
    assert context.endpoint.artifact_promoted_at is None
    assert context.run.state == MarketplaceFeedRun.State.PREPARING
    assert context.run.provider_predecessor_run_id is None
    assert context.run.submitted_at is None


@pytest.mark.parametrize(
    ('setting_name', 'unsafe_value'),
    (
        ('MARKETPLACE_FEED_RUN_MODE', 'legacy'),
        ('MARKETPLACE_FEED_INGRESS_MODE', 'dual_write'),
        ('MARKETPLACE_FEED_ARTIFACT_MODE', 'shadow'),
        ('MARKETPLACE_FEED_STORAGE_MODE', 'stable_bridge'),
    ),
)
def test_every_activation_gate_fails_closed_without_promotion(
    settings,
    setting_name,
    unsafe_value,
):
    context = _attached_generation(f'promotion-gate-{setting_name.lower()}')
    _enable_promotion(settings)
    setattr(settings, setting_name, unsafe_value)

    with pytest.raises(FeedArtifactPromotionConfigurationError):
        _promote(context)

    context.endpoint.refresh_from_db()
    assert context.endpoint.current_artifact_id is None
    assert context.endpoint.artifact_revision == 0


def test_profile_drift_after_external_read_is_rejected(settings):
    context = _attached_generation('promotion-profile-drift')
    expected_revision = context.endpoint.profile_revision
    expected_fingerprint = context.endpoint.profile_fingerprint
    with transaction.atomic():
        MarketplaceFeedEndpoint.objects.select_for_update().filter(
            pk=context.endpoint.pk,
        ).update(
            profile_revision=expected_revision + 1,
            profile_fingerprint='b' * 64,
            profile_verified_at=timezone.now(),
        )
    _enable_promotion(settings)

    with pytest.raises(StaleFeedArtifactPromotion):
        persist_private_feed_promotion_boundary(
            context.claim,
            artifact_id=context.artifact.pk,
            expected_profile_revision=expected_revision,
            expected_profile_fingerprint=expected_fingerprint,
            provider_predecessor_run_id='provider-upload-before',
            submitted_at=timezone.now(),
        )


def test_newer_local_feed_revision_fences_attached_generation_before_submission(
    settings,
):
    context = _attached_generation('promotion-source-drift')
    _enable_promotion(settings)

    with transaction.atomic():
        revision = bump_feed_intents(
            [context.account.pk],
            timezone.now(),
        )[context.account.pk]

    assert revision == 2
    with pytest.raises(StaleFeedArtifactPromotion):
        _promote(context)

    context.endpoint.refresh_from_db()
    context.run.refresh_from_db()
    assert context.endpoint.current_artifact_id is None
    assert context.endpoint.source_intent_revision == 2
    assert context.run.state == MarketplaceFeedRun.State.PREPARING
    assert context.run.submitted_at is None


@pytest.mark.parametrize(
    'drift',
    ('claim', 'source', 'identity', 'storage', 'bucket'),
)
def test_exact_owner_source_claim_and_storage_drift_are_rejected(settings, drift):
    context = _attached_generation(f'promotion-drift-{drift}')
    claim = context.claim
    if drift == 'claim':
        claim = replace(claim, revision=claim.revision + 1)
    elif drift == 'source':
        with transaction.atomic():
            MarketplaceAccount.all_objects.select_for_update().filter(
                pk=context.account.pk,
            ).update(feed_intent_revision=2)
            MarketplaceFeedEndpoint.objects.select_for_update().filter(
                pk=context.endpoint.pk,
            ).update(source_intent_revision=2)
    elif drift == 'identity':
        MarketplaceAccount.all_objects.filter(pk=context.account.pk).update(
            credentials_enc=b'different-opaque-promotion-credentials',
        )
    elif drift == 'storage':
        MarketplaceFeedEndpoint.objects.filter(pk=context.endpoint.pk).update(
            storage_mode=MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
        )
    else:
        settings.MARKETPLACE_FEED_ARTIFACT_BUCKET = 'other-private-feed-artifacts'
    _enable_promotion(settings)

    with pytest.raises(StaleFeedArtifactPromotion):
        persist_private_feed_promotion_boundary(
            claim,
            artifact_id=context.artifact.pk,
            expected_profile_revision=context.endpoint.profile_revision,
            expected_profile_fingerprint=context.endpoint.profile_fingerprint,
            provider_predecessor_run_id='provider-upload-before',
            submitted_at=timezone.now(),
        )

    context.endpoint.refresh_from_db()
    assert context.endpoint.current_artifact_id is None
    assert context.endpoint.artifact_revision == 0


def test_cross_run_artifact_is_rejected_before_pointer_update(settings):
    context = _attached_generation('promotion-cross-artifact-a')
    other = _attached_generation('promotion-cross-artifact-b')
    _enable_promotion(settings)

    with pytest.raises(StaleFeedArtifactPromotion):
        persist_private_feed_promotion_boundary(
            context.claim,
            artifact_id=other.artifact.pk,
            expected_profile_revision=context.endpoint.profile_revision,
            expected_profile_fingerprint=context.endpoint.profile_fingerprint,
            provider_predecessor_run_id='provider-upload-before',
            submitted_at=timezone.now(),
        )

    context.endpoint.refresh_from_db()
    assert context.endpoint.current_artifact_id is None
    assert context.endpoint.artifact_revision == 0
