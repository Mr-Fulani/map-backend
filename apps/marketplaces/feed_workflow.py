"""Durable, provider-neutral orchestration for marketplace feed generations.

The database row is the owner of a feed generation; Celery is only a wake-up
mechanism.  Every worker mutation is fenced by all of generation id, account
identity, lease token, lease deadline, state, and optimistic ``revision``.
Provider adapters deliberately do not belong in this module.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q
from django.utils import timezone

from apps.datasources.encryption import decrypt
from apps.marketplaces.models import Listing, MarketplaceAccount, MarketplaceFeedRun
from apps.sync.models import SyncLog


MAX_POLL_BATCH_SIZE = 100
MAX_RECOVERY_BATCH_SIZE = 100
MAX_GENERATION_LISTINGS = 10_000
MAX_REPORT_PAGES = 100
DEFAULT_LEASE = timedelta(minutes=5)
MAX_ERROR_LENGTH = 2000
PUBLISHED_STATUS_RECHECK_DELAY = timedelta(minutes=10)
UNRESOLVED_POLL_RETRY_DELAY = timedelta(minutes=30)
PROVIDER_RESULT_HORIZON = timedelta(hours=48)
RECONCILIATION_PROVIDER_ACCEPTED = 'provider_accepted'
RECONCILIATION_PROVIDER_NOT_ACCEPTED = 'provider_not_accepted'
FEED_RUN_RECONCILIATION_RESOLUTIONS = (
    RECONCILIATION_PROVIDER_ACCEPTED,
    RECONCILIATION_PROVIDER_NOT_ACCEPTED,
)
OWNER_CHANGE_BLOCK_SUBMITTED = 'block'
OWNER_CHANGE_HOLD_SUBMITTED = 'outcome_uncertain'
OWNER_CHANGE_SUBMITTED_POLICIES = (
    OWNER_CHANGE_BLOCK_SUBMITTED,
    OWNER_CHANGE_HOLD_SUBMITTED,
)


class FeedWorkflowError(RuntimeError):
    """Base class for feed state-machine failures."""


class FeedAccountUnavailable(FeedWorkflowError):
    """The requested account does not exist or cannot own provider work."""


class FeedRunConflict(FeedWorkflowError):
    """The requested transition is not valid for the current run state."""


class FeedSubmissionOutcomeUncertain(FeedRunConflict):
    """An unresolved ambiguous POST blocks every later feed for the account."""


class StaleFeedRunClaim(FeedWorkflowError):
    """A delayed worker no longer owns the exact run revision and lease."""


@dataclass(frozen=True, slots=True)
class FeedRunSnapshot:
    """Immutable state returned after every durable transition."""

    run_id: uuid.UUID
    account_id: int
    tenant_id: int
    marketplace: str
    state: str
    revision: int
    account_identity_digest: str
    payload_sha256: str
    submitted_at: datetime | None
    provider_result_deadline_at: datetime | None
    created_at: datetime
    submission_reconcile_attempt: int
    poll_cursor_listing_id: int
    poll_round: int
    report_page: int
    report_attempt: int
    report_completed_at: datetime | None
    next_attempt_at: datetime | None
    total_count: int
    published_count: int
    rejected_count: int
    pending_count: int
    provider_run_id: str | None
    provider_predecessor_run_id: str | None
    finished_at: datetime | None

    @property
    def generation_id(self) -> uuid.UUID:
        return self.run_id

    @property
    def id(self) -> uuid.UUID:
        return self.run_id

    @property
    def pk(self) -> uuid.UUID:
        return self.run_id

    @property
    def other_resolved_count(self) -> int:
        """Rows resolved into a state other than published/rejected/pending."""

        value = (
            self.total_count
            - self.published_count
            - self.rejected_count
            - self.pending_count
        )
        if value < 0:
            raise FeedRunConflict('Feed counters exceed the immutable generation total.')
        return value


@dataclass(frozen=True, slots=True)
class FeedRunClaim(FeedRunSnapshot):
    """An exact, short-lived ownership capability for one provider step."""

    claim_token: uuid.UUID
    claimed_until: datetime


@dataclass(frozen=True, slots=True)
class FeedPageApplyResult:
    """Committed result of one provider page application.

    Counts are page deltas, while ``snapshot`` contains the durable aggregate
    counters.  Returning listing ids lets a caller build one bounded digest
    without coupling this persistence layer to a notification provider.
    """

    snapshot: FeedRunSnapshot
    changed_listing_ids: tuple[int, ...]
    published_count: int = 0
    rejected_count: int = 0

    @property
    def changed_count(self) -> int:
        return len(self.changed_listing_ids)


def account_identity_digest(account: MarketplaceAccount) -> str:
    """Return a stable non-secret digest of the provider account generation.

    Fernet ciphertext is randomized, so hashing ``credentials_enc`` directly
    would supersede an in-flight run after a no-op key rotation.  Decrypt valid
    credentials, canonicalize their JSON representation, and authenticate the
    complete identity with a domain-separated key derived from ``SECRET_KEY``.
    This detects a semantic credential change without storing or logging a
    low-entropy client secret's plain hash.

    Legacy or corrupt opaque values cannot be interpreted semantically.  They
    deliberately use a separate ciphertext-bound fallback: it is deterministic
    for the same stored bytes, but any byte change fences the old generation.
    """

    encrypted_credentials = bytes(account.credentials_enc or b'')
    try:
        credentials = decrypt(encrypted_credentials)
        if not isinstance(credentials, dict):
            raise ValueError('Marketplace credentials must decrypt to an object.')
        credential_source = b'canonical-json'
        credential_identity = json.dumps(
            credentials,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
    except Exception:
        # Fail closed for pre-encryption fixtures, legacy rows, and damaged
        # ciphertext.  Never include either representation in an exception,
        # log record, model field, or return value.
        credential_source = b'opaque-ciphertext'
        credential_identity = encrypted_credentials

    account_identity = json.dumps(
        {
            'account_id': account.pk,
            'external_id': account.external_id,
            'marketplace': account.marketplace,
            'tenant_id': account.tenant_id,
        },
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')
    root_key = str(settings.SECRET_KEY).encode('utf-8')
    hmac_key = hmac.new(
        root_key,
        b'saas-poster:marketplace-feed-account-identity:v1',
        hashlib.sha256,
    ).digest()
    authenticated_identity = b'\x00'.join(
        (
            b'v1',
            account_identity,
            credential_source,
            credential_identity,
        ),
    )
    return hmac.new(hmac_key, authenticated_identity, hashlib.sha256).hexdigest()


def _now(value: datetime | None) -> datetime:
    value = value or timezone.now()
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError('now must be a timezone-aware datetime.')
    return value


def _future(value: datetime, *, field_name: str, after: datetime | None = None) -> datetime:
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValueError(f'{field_name} must be a timezone-aware datetime.')
    if after is not None and value <= after:
        raise ValueError(f'{field_name} must be later than now.')
    return value


def _lease_duration(value: timedelta) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError('lease must be a positive timedelta.')
    return value


def _digest(value: str, *, field_name: str, allow_blank: bool = False) -> str:
    if value == '' and allow_blank:
        return value
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f'{field_name} must be a 64-character hexadecimal SHA-256 digest.')
    try:
        int(value, 16)
    except ValueError:
        raise ValueError(f'{field_name} must be a 64-character hexadecimal SHA-256 digest.') from None
    return value.casefold()


def _error(value: object) -> str:
    text = str(value or '').replace('\x00', '').replace('\r', ' ').replace('\n', ' ')
    return ' '.join(text.split())[:MAX_ERROR_LENGTH]


def _report_reason(value: object) -> str:
    raw = html.unescape(str(value or ''))
    without_tags = re.sub(r'<[^>]+>', ' ', raw)
    without_controls = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', without_tags)
    normalized = re.sub(r'\s+', ' ', without_controls).strip()
    return normalized[:MAX_ERROR_LENGTH].rstrip()


def _snapshot(run: MarketplaceFeedRun) -> FeedRunSnapshot:
    return FeedRunSnapshot(
        run_id=run.pk,
        account_id=run.account_id,
        tenant_id=run.tenant_id,
        marketplace=run.marketplace,
        state=run.state,
        revision=run.revision,
        account_identity_digest=run.account_identity_digest,
        payload_sha256=run.payload_sha256,
        submitted_at=run.submitted_at,
        provider_result_deadline_at=run.provider_result_deadline_at,
        created_at=run.created_at,
        submission_reconcile_attempt=run.submission_reconcile_attempt,
        poll_cursor_listing_id=run.poll_cursor_listing_id,
        poll_round=run.poll_round,
        report_page=run.report_page,
        report_attempt=run.report_attempt,
        report_completed_at=run.report_completed_at,
        next_attempt_at=run.next_attempt_at,
        total_count=run.total_count,
        published_count=run.published_count,
        rejected_count=run.rejected_count,
        pending_count=run.pending_count,
        provider_run_id=run.provider_run_id,
        provider_predecessor_run_id=run.provider_predecessor_run_id,
        finished_at=run.finished_at,
    )


def _claim_snapshot(run: MarketplaceFeedRun) -> FeedRunClaim:
    if run.claim_token is None or run.claimed_until is None:
        raise FeedRunConflict('The feed run is not claimed.')
    values = _snapshot(run)
    return FeedRunClaim(
        **{field: getattr(values, field) for field in values.__dataclass_fields__},
        claim_token=run.claim_token,
        claimed_until=run.claimed_until,
    )


def _live(account: MarketplaceAccount) -> bool:
    return (
        account.deleted_at is None
        and account.is_active
        and account.tenant.is_active
    )


def _lock_account(account_id: int) -> MarketplaceAccount:
    try:
        return (
            MarketplaceAccount.all_objects.select_for_update(of=('self',))
            .select_related('tenant')
            .get(pk=account_id)
        )
    except MarketplaceAccount.DoesNotExist:
        raise FeedAccountUnavailable(f'Marketplace account {account_id} does not exist.') from None


def _terminalize_locked(
    run: MarketplaceFeedRun,
    *,
    state: str,
    now: datetime,
    error: object = '',
) -> FeedRunSnapshot:
    if state not in MarketplaceFeedRun.TERMINAL_STATES:
        raise ValueError('state must be terminal.')
    if run.state in MarketplaceFeedRun.TERMINAL_STATES:
        return _snapshot(run)
    changed = MarketplaceFeedRun.objects.filter(
        pk=run.pk,
        account_id=run.account_id,
        state=run.state,
        revision=run.revision,
    ).update(
        state=state,
        revision=F('revision') + 1,
        next_attempt_at=None,
        claim_token=None,
        claimed_until=None,
        last_error=_error(error),
        finished_at=now,
        updated_at=now,
    )
    if changed != 1:
        raise FeedRunConflict('Feed run changed during terminal transition.')
    run.refresh_from_db()
    return _snapshot(run)


def _has_proven_no_submission(run: MarketplaceFeedRun) -> bool:
    """Return whether an owner can be released without provider ambiguity."""

    return (
        run.state == MarketplaceFeedRun.State.PREPARING
        and run.submitted_at is None
        and not run.provider_run_id
    )


def _fence_invalid_owner_locked(
    run: MarketplaceFeedRun,
    *,
    safe_state: str,
    now: datetime,
    error: object,
) -> FeedRunSnapshot:
    """Fence invalid ownership without freeing a possibly submitted account."""

    if _has_proven_no_submission(run):
        return _terminalize_locked(
            run,
            state=safe_state,
            now=now,
            error=error,
        )
    return _terminalize_locked(
        run,
        state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
        now=now,
        error=(
            f'{_error(error)} Provider submission may already have been accepted; '
            'manual reconciliation is required.'
        ),
    )


@transaction.atomic
def fence_account_feed_runs_for_owner_change(
    account_id: int,
    *,
    reason: object,
    safe_state: str = MarketplaceFeedRun.State.SUPERSEDED,
    submitted_policy: str = OWNER_CHANGE_BLOCK_SUBMITTED,
    now: datetime | None = None,
) -> FeedRunSnapshot | None:
    """Fence the account owner before an identity or availability mutation.

    The function always acquires the account lock before the run lock, so it
    is safe to call inside a service transaction that already owns the same
    account row.  A never-submitted PREPARING owner can be closed safely.  A
    submitted or ambiguous owner is either rejected with a conflict or moved
    to the durable ``outcome_uncertain`` account hold, according to the
    explicit caller policy.
    """

    transition_at = _now(now)
    if safe_state not in {
        MarketplaceFeedRun.State.SUPERSEDED,
        MarketplaceFeedRun.State.CANCELLED,
    }:
        raise ValueError('safe_state must be superseded or cancelled.')
    if submitted_policy not in OWNER_CHANGE_SUBMITTED_POLICIES:
        raise ValueError(
            'submitted_policy must be block or outcome_uncertain.',
        )
    account = _lock_account(account_id)
    run = (
        MarketplaceFeedRun.objects.select_for_update()
        .filter(
            account_id=account.pk,
            state__in=MarketplaceFeedRun.OWNERSHIP_STATES,
        )
        .order_by('created_at', 'pk')
        .first()
    )
    if run is None:
        return None
    if _has_proven_no_submission(run):
        return _terminalize_locked(
            run,
            state=safe_state,
            now=transition_at,
            error=reason,
        )
    if submitted_policy == OWNER_CHANGE_BLOCK_SUBMITTED:
        raise FeedSubmissionOutcomeUncertain(
            'A provider submission already owns this account; reconcile it '
            'before changing marketplace account identity.',
        )
    if run.state == MarketplaceFeedRun.State.OUTCOME_UNCERTAIN:
        return _snapshot(run)
    return _fence_invalid_owner_locked(
        run,
        safe_state=safe_state,
        now=transition_at,
        error=reason,
    )


def assert_no_submitted_feed_owner(
    account_id: int,
    *,
    reason: object,
    now: datetime | None = None,
) -> FeedRunSnapshot | None:
    """Allow an identity mutation only after fencing safe PREPARING work."""

    return fence_account_feed_runs_for_owner_change(
        account_id,
        reason=reason,
        safe_state=MarketplaceFeedRun.State.SUPERSEDED,
        submitted_policy=OWNER_CHANGE_BLOCK_SUBMITTED,
        now=now,
    )


@transaction.atomic
def create_or_supersede_feed_run(
    account_id: int,
    *,
    generation_id: uuid.UUID | None = None,
    payload_sha256: str = '',
    source_intent_revision: int | None = None,
    endpoint_revision: int | None = None,
    predecessor_artifact_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Create one generation and stamp all currently pending feed listings.

    Replaying the same ``generation_id`` is idempotent.  A genuinely new
    generation may replace only an unclaimed PREPARING generation that has no
    evidence of submission.  Submitted or ambiguous work is never superseded.
    """

    transition_at = _now(now)
    payload_sha256 = _digest(payload_sha256, field_name='payload_sha256', allow_blank=True)
    private_generation = source_intent_revision is not None
    if private_generation:
        if (
            isinstance(source_intent_revision, bool)
            or not isinstance(source_intent_revision, int)
            or source_intent_revision < 1
            or isinstance(endpoint_revision, bool)
            or not isinstance(endpoint_revision, int)
            or endpoint_revision < 0
        ):
            raise ValueError(
                'Private generation revisions must be non-negative exact integers.',
            )
        if predecessor_artifact_id is not None and not isinstance(
            predecessor_artifact_id,
            uuid.UUID,
        ):
            raise ValueError('predecessor_artifact_id must be a UUID or null.')
    elif endpoint_revision is not None or predecessor_artifact_id is not None:
        raise ValueError(
            'Private generation fields must be supplied as one bundle.',
        )
    generation_id = generation_id or uuid.uuid4()
    if not isinstance(generation_id, uuid.UUID):
        try:
            generation_id = uuid.UUID(str(generation_id))
        except (TypeError, ValueError):
            raise ValueError('generation_id must be a UUID.') from None

    account = _lock_account(account_id)
    if not _live(account):
        raise FeedAccountUnavailable(f'Marketplace account {account_id} is inactive or deleted.')

    existing = MarketplaceFeedRun.objects.select_for_update().filter(pk=generation_id).first()
    if existing is not None:
        if existing.account_id != account.pk:
            raise FeedRunConflict('Generation id already belongs to another account.')
        if payload_sha256 and existing.payload_sha256 not in ('', payload_sha256):
            raise FeedRunConflict('Generation replay has a different payload digest.')
        if (
            existing.source_intent_revision != source_intent_revision
            or existing.endpoint_revision != endpoint_revision
            or existing.predecessor_artifact_id != predecessor_artifact_id
        ):
            raise FeedRunConflict(
                'Generation replay has a different private endpoint snapshot.',
            )
        return _snapshot(existing)

    pending_rows = Listing.objects.filter(
        account_id=account.pk,
        tenant_id=account.tenant_id,
        status=Listing.STATUS_PENDING,
        external_id__isnull=True,
    )
    pending_count = pending_rows.order_by().values('pk')[:MAX_GENERATION_LISTINGS + 1].count()
    if pending_count > MAX_GENERATION_LISTINGS:
        raise FeedRunConflict(
            f'A feed generation cannot contain more than {MAX_GENERATION_LISTINGS} listings.',
        )
    uncertain_generation_id = (
        MarketplaceFeedRun.objects.select_for_update()
        .filter(
            account_id=account.pk,
            state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
        )
        .values_list('pk', flat=True)
        .first()
    )
    if uncertain_generation_id is not None:
        raise FeedSubmissionOutcomeUncertain(
            'A previous provider submission has an uncertain outcome and still '
            f'owns this account ({uncertain_generation_id}); manual '
            'reconciliation is required before another feed POST.',
        )

    active_runs = list(
        MarketplaceFeedRun.objects.select_for_update()
        .filter(account_id=account.pk, state__in=MarketplaceFeedRun.ACTIVE_STATES)
        .order_by('created_at', 'pk')
    )
    for active_run in active_runs:
        # Once a worker owns PREPARING it may already be crossing the provider
        # boundary.  Every later state is likewise submitted or ambiguous, so
        # only an unclaimed, never-submitted PREPARING row is safe to replace.
        safely_replaceable = (
            active_run.state == MarketplaceFeedRun.State.PREPARING
            and active_run.claim_token is None
            and active_run.claimed_until is None
            and active_run.submitted_at is None
            and active_run.provider_run_id is None
        )
        if not safely_replaceable:
            raise FeedRunConflict(
                'An active feed generation may already have crossed the provider boundary.',
            )
        _terminalize_locked(
            active_run,
            state=MarketplaceFeedRun.State.SUPERSEDED,
            now=transition_at,
            error=f'Superseded by feed generation {generation_id}.',
        )

    run = MarketplaceFeedRun.objects.create(
        id=generation_id,
        tenant_id=account.tenant_id,
        account_id=account.pk,
        marketplace=account.marketplace,
        account_identity_digest=account_identity_digest(account),
        payload_sha256=payload_sha256,
        source_intent_revision=source_intent_revision,
        endpoint_revision=endpoint_revision,
        predecessor_artifact_id=predecessor_artifact_id,
        state=MarketplaceFeedRun.State.PREPARING,
        next_attempt_at=transition_at,
    )
    tagged_count = pending_rows.update(feed_run_id=run.pk, updated_at=transition_at)
    if tagged_count != pending_count:
        raise FeedRunConflict('Feed generation membership changed while it was being created.')
    MarketplaceFeedRun.objects.filter(pk=run.pk, revision=0).update(
        total_count=tagged_count,
        pending_count=tagged_count,
        updated_at=transition_at,
    )
    run.refresh_from_db()
    return _snapshot(run)


create_feed_run = create_or_supersede_feed_run


@transaction.atomic
def resume_failed_pre_submission_feed_run(
    account_id: int,
    *,
    generation_id: uuid.UUID,
    expected_revision: int,
    payload_sha256: str,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Reopen one exact private generation only when no boundary was crossed.

    A private source revision is unique per account, so a safely failed run
    cannot be replaced by a second UUID.  Reusing its UUID is allowed only
    when every storage/provider evidence field is still pristine and all of
    its pending listing membership is unchanged.
    """

    transition_at = _now(now)
    payload_sha256 = _digest(
        payload_sha256,
        field_name='payload_sha256',
        allow_blank=False,
    )
    if isinstance(expected_revision, bool) or not isinstance(
        expected_revision,
        int,
    ) or expected_revision < 0:
        raise ValueError('expected_revision must be a non-negative integer.')
    if not isinstance(generation_id, uuid.UUID):
        try:
            generation_id = uuid.UUID(str(generation_id))
        except (TypeError, ValueError):
            raise ValueError('generation_id must be a UUID.') from None

    account = _lock_account(account_id)
    if not _live(account):
        raise FeedAccountUnavailable(
            f'Marketplace account {account_id} is inactive or deleted.',
        )
    run = (
        MarketplaceFeedRun.objects.select_for_update()
        .filter(pk=generation_id, account_id=account.pk)
        .first()
    )
    if run is None:
        raise FeedRunConflict('Failed feed generation no longer exists.')

    safe_pre_submission_failure = (
        run.state == MarketplaceFeedRun.State.FAILED
        and run.revision == expected_revision
        and run.source_intent_revision is not None
        and run.payload_sha256 == payload_sha256
        and run.account_identity_digest == account_identity_digest(account)
        and run.claim_token is None
        and run.claimed_until is None
        and run.submitted_at is None
        and run.provider_run_id is None
        and run.provider_predecessor_run_id is None
        and run.provider_result_deadline_at is None
        and run.submission_reconcile_attempt == 0
        and run.feed_artifact_id is None
        and run.artifact_upload_attempt == 0
        and run.published_count == 0
        and run.rejected_count == 0
        and run.pending_count == run.total_count
        and run.poll_cursor_listing_id == 0
        and run.poll_round == 0
        and run.report_page == 1
        and run.report_attempt == 0
        and run.report_completed_at is None
        and run.finished_at is not None
    )
    if not safe_pre_submission_failure:
        raise FeedRunConflict(
            'Failed feed generation has storage, provider, ownership, or '
            'result evidence and cannot be replayed automatically.',
        )
    if run.artifact_upload_attempts.exists():
        raise FeedRunConflict(
            'Failed feed generation has a durable object upload attempt.',
        )
    tagged_pending_count = Listing.objects.filter(
        feed_run_id=run.pk,
        account_id=account.pk,
        tenant_id=account.tenant_id,
        status=Listing.STATUS_PENDING,
        external_id__isnull=True,
    ).count()
    if tagged_pending_count != run.pending_count:
        raise FeedRunConflict(
            'Failed feed generation membership changed before safe replay.',
        )

    run.state = MarketplaceFeedRun.State.PREPARING
    run.revision += 1
    run.next_attempt_at = transition_at
    run.last_error = ''
    run.finished_at = None
    run.updated_at = transition_at
    run.save(update_fields=[
        'state',
        'revision',
        'next_attempt_at',
        'last_error',
        'finished_at',
        'updated_at',
    ])
    return _snapshot(run)


def _claim_locked_account(
    account: MarketplaceAccount,
    *,
    now: datetime,
    lease: timedelta,
    expected_generation_id: uuid.UUID | None = None,
    expected_revision: int | None = None,
) -> FeedRunClaim | None:
    runs = MarketplaceFeedRun.objects.select_for_update().filter(
        account_id=account.pk,
        state__in=MarketplaceFeedRun.ACTIVE_STATES,
    )
    if expected_generation_id is not None:
        runs = runs.filter(pk=expected_generation_id)
    run = runs.order_by('created_at', 'pk').first()
    if run is None:
        return None

    current_identity = account_identity_digest(account)
    if not _live(account):
        _fence_invalid_owner_locked(
            run,
            safe_state=MarketplaceFeedRun.State.CANCELLED,
            now=now,
            error='Marketplace account is inactive or deleted.',
        )
        return None
    if run.tenant_id != account.tenant_id or run.marketplace != account.marketplace:
        _fence_invalid_owner_locked(
            run,
            safe_state=MarketplaceFeedRun.State.SUPERSEDED,
            now=now,
            error='Marketplace account ownership changed.',
        )
        return None
    if run.account_identity_digest != current_identity:
        _fence_invalid_owner_locked(
            run,
            safe_state=MarketplaceFeedRun.State.SUPERSEDED,
            now=now,
            error='Marketplace account identity changed.',
        )
        return None
    if expected_revision is not None and run.revision != expected_revision:
        return None
    if run.next_attempt_at is None or run.next_attempt_at > now:
        return None
    if run.claim_token is not None and run.claimed_until is not None and run.claimed_until > now:
        return None

    token = uuid.uuid4()
    lease_until = now + lease
    changed = MarketplaceFeedRun.objects.filter(
        pk=run.pk,
        account_id=account.pk,
        tenant_id=account.tenant_id,
        marketplace=account.marketplace,
        account_identity_digest=current_identity,
        state=run.state,
        revision=run.revision,
        next_attempt_at=run.next_attempt_at,
        claim_token=run.claim_token,
        claimed_until=run.claimed_until,
    ).filter(
        Q(claim_token__isnull=True)
        | Q(claimed_until__isnull=True)
        | Q(claimed_until__lte=now)
    ).update(
        revision=F('revision') + 1,
        claim_token=token,
        claimed_until=lease_until,
        updated_at=now,
    )
    if changed != 1:
        return None
    run.refresh_from_db()
    return _claim_snapshot(run)


@transaction.atomic
def claim_due_run_for_account(
    account_id: int,
    *,
    expected_generation_id: uuid.UUID | None = None,
    expected_revision: int | None = None,
    now: datetime | None = None,
    lease: timedelta = DEFAULT_LEASE,
) -> FeedRunClaim | None:
    """Claim the one due generation for an account with a renewable lease."""

    claimed_at = _now(now)
    lease = _lease_duration(lease)
    try:
        account = _lock_account(account_id)
    except FeedAccountUnavailable:
        return None
    return _claim_locked_account(
        account,
        now=claimed_at,
        lease=lease,
        expected_generation_id=expected_generation_id,
        expected_revision=expected_revision,
    )


@transaction.atomic
def claim_due_runs(
    *,
    limit: int = MAX_RECOVERY_BATCH_SIZE,
    marketplace: str | None = None,
    now: datetime | None = None,
    lease: timedelta = DEFAULT_LEASE,
) -> tuple[FeedRunClaim, ...]:
    """Claim a bounded recovery batch while skipping accounts owned elsewhere."""

    claimed_at = _now(now)
    lease = _lease_duration(lease)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECOVERY_BATCH_SIZE:
        raise ValueError(f'limit must be between 1 and {MAX_RECOVERY_BATCH_SIZE}.')

    due_runs = MarketplaceFeedRun.objects.filter(
        account_id=OuterRef('pk'),
        state__in=MarketplaceFeedRun.ACTIVE_STATES,
        next_attempt_at__isnull=False,
        next_attempt_at__lte=claimed_at,
    ).filter(
        Q(claim_token__isnull=True)
        | Q(claimed_until__isnull=True)
        | Q(claimed_until__lte=claimed_at)
    )
    if marketplace is not None:
        due_runs = due_runs.filter(marketplace=marketplace)

    accounts = MarketplaceAccount.all_objects.annotate(
        has_due_feed_run=Exists(due_runs),
    ).filter(
        deleted_at__isnull=True,
        is_active=True,
        tenant__is_active=True,
        has_due_feed_run=True,
    )
    if marketplace is not None:
        accounts = accounts.filter(marketplace=marketplace)
    accounts = accounts.select_for_update(skip_locked=True, of=('self',)).order_by('pk')[:limit]

    claims: list[FeedRunClaim] = []
    for account in accounts:
        claim = _claim_locked_account(account, now=claimed_at, lease=lease)
        if claim is not None:
            claims.append(claim)
    return tuple(claims)


def _cancel_inactive_feed_run(
    run_id: uuid.UUID,
    account_id: int,
    *,
    now: datetime,
) -> FeedRunSnapshot | None:
    """Cancel one orphan while preserving account -> run lock ordering."""

    with transaction.atomic():
        try:
            account = _lock_account(account_id)
        except FeedAccountUnavailable:
            return None
        run = (
            MarketplaceFeedRun.objects.select_for_update()
            .filter(
                pk=run_id,
                account_id=account.pk,
                state__in=MarketplaceFeedRun.ACTIVE_STATES,
            )
            .first()
        )
        if run is None or _live(account):
            return None
        return _fence_invalid_owner_locked(
            run,
            safe_state=MarketplaceFeedRun.State.CANCELLED,
            now=now,
            error='Marketplace account or tenant is inactive or deleted.',
        )


def cancel_feed_runs_for_inactive_owners(
    *,
    limit: int = MAX_RECOVERY_BATCH_SIZE,
    now: datetime | None = None,
) -> tuple[FeedRunSnapshot, ...]:
    """Cancel a bounded batch whose account or tenant can no longer own work.

    Candidate discovery is intentionally lock-free.  Each candidate is then
    revalidated in its own short account -> run transaction, so no provider
    call happens and one contended account cannot hold locks for the full batch.
    """

    cancelled_at = _now(now)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RECOVERY_BATCH_SIZE:
        raise ValueError(f'limit must be between 1 and {MAX_RECOVERY_BATCH_SIZE}.')
    candidates = tuple(
        MarketplaceFeedRun.objects.filter(
            state__in=MarketplaceFeedRun.ACTIVE_STATES,
        ).filter(
            Q(account__deleted_at__isnull=False)
            | Q(account__is_active=False)
            | Q(account__tenant__is_active=False),
        ).order_by('account_id', 'created_at', 'pk').values_list(
            'pk', 'account_id',
        )[:limit]
    )
    cancelled: list[FeedRunSnapshot] = []
    for run_id, account_id in candidates:
        snapshot = _cancel_inactive_feed_run(
            run_id,
            account_id,
            now=cancelled_at,
        )
        if snapshot is not None:
            cancelled.append(snapshot)
    return tuple(cancelled)


def _validate_claim_for_read(
    claim: FeedRunClaim,
    *,
    allowed_states: Iterable[str],
    now: datetime,
) -> MarketplaceFeedRun:
    run = MarketplaceFeedRun.objects.select_related('account__tenant').filter(
        pk=claim.run_id,
        account_id=claim.account_id,
        tenant_id=claim.tenant_id,
        marketplace=claim.marketplace,
        account_identity_digest=claim.account_identity_digest,
        state=claim.state,
        state__in=tuple(allowed_states),
        revision=claim.revision,
        claim_token=claim.claim_token,
        claimed_until=claim.claimed_until,
        claimed_until__gt=now,
        account__deleted_at__isnull=True,
        account__is_active=True,
        account__tenant__is_active=True,
    ).first()
    if (
        run is None
        or not _live(run.account)
        or account_identity_digest(run.account) != claim.account_identity_digest
    ):
        raise StaleFeedRunClaim('Feed run claim is stale.')
    return run


def load_poll_batch(
    claim: FeedRunClaim,
    *,
    limit: int = MAX_POLL_BATCH_SIZE,
    now: datetime | None = None,
) -> tuple[Listing, ...]:
    """Load one keyset page; provider work must never exceed 100 listings."""

    checked_at = _now(now)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_POLL_BATCH_SIZE:
        raise ValueError(f'limit must be between 1 and {MAX_POLL_BATCH_SIZE}.')
    run = _validate_claim_for_read(
        claim,
        allowed_states=(MarketplaceFeedRun.State.POLLING,),
        now=checked_at,
    )
    if run.poll_cursor_listing_id != claim.poll_cursor_listing_id:
        raise StaleFeedRunClaim('Poll cursor changed after claim.')
    return tuple(
        Listing.objects.filter(
            feed_run_id=run.pk,
            account_id=run.account_id,
            status=Listing.STATUS_PENDING,
            external_id__isnull=True,
            pk__gt=run.poll_cursor_listing_id,
        ).order_by('pk')[:limit]
    )


def _lock_claimed_run(
    claim: FeedRunClaim,
    *,
    allowed_states: Iterable[str],
    now: datetime,
) -> MarketplaceFeedRun:
    try:
        account = _lock_account(claim.account_id)
    except FeedAccountUnavailable:
        raise StaleFeedRunClaim('Feed account no longer exists.') from None
    if not _live(account) or account_identity_digest(account) != claim.account_identity_digest:
        raise StaleFeedRunClaim('Feed account identity is no longer live.')

    run = MarketplaceFeedRun.objects.select_for_update().filter(
        pk=claim.run_id,
        account_id=claim.account_id,
        tenant_id=claim.tenant_id,
        marketplace=claim.marketplace,
        account_identity_digest=claim.account_identity_digest,
        state=claim.state,
        state__in=tuple(allowed_states),
        revision=claim.revision,
        claim_token=claim.claim_token,
        claimed_until=claim.claimed_until,
        claimed_until__gt=now,
    ).first()
    if run is None:
        raise StaleFeedRunClaim('Feed run claim is stale or its lease expired.')
    return run


def _transition_claimed(
    run: MarketplaceFeedRun,
    claim: FeedRunClaim,
    *,
    now: datetime,
    updates: dict[str, object],
) -> FeedRunSnapshot:
    updates = {
        **updates,
        'revision': F('revision') + 1,
        'claim_token': None,
        'claimed_until': None,
        'updated_at': now,
    }
    changed = MarketplaceFeedRun.objects.filter(
        pk=claim.run_id,
        account_id=claim.account_id,
        tenant_id=claim.tenant_id,
        marketplace=claim.marketplace,
        account_identity_digest=claim.account_identity_digest,
        state=claim.state,
        revision=claim.revision,
        claim_token=claim.claim_token,
        claimed_until=claim.claimed_until,
        claimed_until__gt=now,
    ).update(**updates)
    if changed != 1:
        raise StaleFeedRunClaim('Feed run changed before the transition was committed.')
    run.refresh_from_db()
    return _snapshot(run)


def _counter_values(
    run: MarketplaceFeedRun,
    *,
    published_delta: int,
    rejected_delta: int,
    other_resolved_delta: int,
) -> dict[str, int]:
    deltas = (published_delta, rejected_delta, other_resolved_delta)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in deltas):
        raise ValueError('counter deltas must be non-negative integers.')
    resolved = sum(deltas)
    if resolved > run.pending_count:
        raise FeedRunConflict('Resolved counter delta exceeds the pending count.')
    published = run.published_count + published_delta
    rejected = run.rejected_count + rejected_delta
    pending = run.pending_count - resolved
    if published + rejected + pending > run.total_count:
        raise FeedRunConflict('Feed counters exceed the generation total.')
    return {
        'published_count': published,
        'rejected_count': rejected,
        'pending_count': pending,
    }


def _recomputed_counters(run: MarketplaceFeedRun) -> dict[str, int]:
    """Derive mutable counters while preserving immutable ``total_count``."""

    values = Listing.all_objects.filter(
        feed_run_id=run.pk,
        account_id=run.account_id,
        tenant_id=run.tenant_id,
    ).aggregate(
        published_count=Count('pk', filter=Q(status=Listing.STATUS_ACTIVE)),
        rejected_count=Count('pk', filter=Q(status=Listing.STATUS_REJECTED)),
        pending_count=Count('pk', filter=Q(status=Listing.STATUS_PENDING)),
    )
    counters = {name: int(value or 0) for name, value in values.items()}
    if sum(counters.values()) > run.total_count:
        raise FeedRunConflict(
            'Canonical listing counters exceed the immutable generation total.',
        )
    return counters


def _poll_listing_ids(values: Iterable[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError('batch_listing_ids must be an ordered iterable of listing ids.')
    try:
        listing_ids = tuple(values)
    except TypeError:
        raise ValueError('batch_listing_ids must be an ordered iterable of listing ids.') from None
    if not 1 <= len(listing_ids) <= MAX_POLL_BATCH_SIZE:
        raise ValueError(f'batch_listing_ids must contain between 1 and {MAX_POLL_BATCH_SIZE} ids.')
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in listing_ids):
        raise ValueError('batch_listing_ids must contain positive integer ids.')
    if any(left >= right for left, right in zip(listing_ids, listing_ids[1:])):
        raise ValueError('batch_listing_ids must be unique and strictly increasing.')
    return listing_ids


def _resolved_external_ids(
    values: Mapping[int, str],
    *,
    batch_listing_ids: tuple[int, ...],
) -> dict[int, str]:
    if not isinstance(values, Mapping) or len(values) > MAX_POLL_BATCH_SIZE:
        raise ValueError(f'resolved_external_ids must be a mapping of at most {MAX_POLL_BATCH_SIZE} rows.')
    batch_id_set = set(batch_listing_ids)
    result: dict[int, str] = {}
    for listing_id, raw_external_id in values.items():
        if isinstance(listing_id, bool) or not isinstance(listing_id, int):
            raise ValueError('resolved_external_ids keys must be integer listing ids.')
        if listing_id not in batch_id_set:
            raise ValueError('resolved_external_ids contains a listing outside the exact poll batch.')
        if not isinstance(raw_external_id, str):
            raise ValueError('A resolved provider listing id must be a string.')
        external_id = raw_external_id.strip()
        if not external_id or len(external_id) > 100:
            raise ValueError('A resolved provider listing id must contain 1 to 100 characters.')
        result[listing_id] = external_id
    return result


def _normalized_report_errors(values: Mapping[Any, object]) -> dict[uuid.UUID, str]:
    if not isinstance(values, Mapping) or len(values) > MAX_POLL_BATCH_SIZE:
        raise ValueError(f'errors_by_ad_id must be a mapping of at most {MAX_POLL_BATCH_SIZE} rows.')
    result: dict[uuid.UUID, str] = {}
    for raw_ad_id, raw_reason in values.items():
        if isinstance(raw_ad_id, uuid.UUID):
            ad_id = raw_ad_id
        else:
            try:
                ad_id = uuid.UUID(str(raw_ad_id))
            except (AttributeError, TypeError, ValueError):
                raise ValueError('errors_by_ad_id keys must be UUID values.') from None
        if ad_id in result:
            raise ValueError('errors_by_ad_id contains the same UUID more than once.')
        result[ad_id] = _report_reason(raw_reason)
    return result


def _normalized_report_external_ids(
    values: Mapping[Any, object],
) -> dict[uuid.UUID, str]:
    if not isinstance(values, Mapping) or len(values) > MAX_POLL_BATCH_SIZE:
        raise ValueError(
            'external_ids_by_ad_id must be a mapping of at most '
            f'{MAX_POLL_BATCH_SIZE} rows.',
        )
    result: dict[uuid.UUID, str] = {}
    for raw_ad_id, raw_external_id in values.items():
        if isinstance(raw_ad_id, uuid.UUID):
            ad_id = raw_ad_id
        else:
            try:
                ad_id = uuid.UUID(str(raw_ad_id))
            except (AttributeError, TypeError, ValueError):
                raise ValueError(
                    'external_ids_by_ad_id keys must be UUID values.',
                ) from None
        if ad_id in result:
            raise ValueError(
                'external_ids_by_ad_id contains the same UUID more than once.',
            )
        if not isinstance(raw_external_id, str):
            raise ValueError('A resolved provider listing id must be a string.')
        external_id = raw_external_id.strip()
        if not external_id or len(external_id) > 100:
            raise ValueError(
                'A resolved provider listing id must contain 1 to 100 characters.',
            )
        result[ad_id] = external_id
    return result


def _provider_run_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError('provider_run_id must be a string.')
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError('provider_run_id must contain 1 to 200 characters.')
    return normalized


def _provider_predecessor_run_id(value: object) -> str:
    """Normalize an authoritative pre-POST provider baseline.

    The empty string means the strict provider read proved that no predecessor
    existed. ``None`` means no baseline was captured, so it is intentionally
    rejected at the submission boundary.
    """

    if not isinstance(value, str):
        raise ValueError('provider_predecessor_run_id must be a string.')
    normalized = value.strip()
    if len(normalized) > 200:
        raise ValueError('provider_predecessor_run_id must be at most 200 characters.')
    return normalized


@transaction.atomic
def persist_feed_submission_boundary(
    claim: FeedRunClaim,
    *,
    provider_predecessor_run_id: str,
    submitted_at: datetime,
    now: datetime | None = None,
) -> FeedRunClaim | None:
    """Revalidate the frozen owner and persist the boundary before the POST.

    The account lock is acquired after the immutable feed object was uploaded
    and immediately before the non-idempotent provider call.  Invalid owners
    are fenced while this transaction commits; returning ``None`` tells the
    caller that no POST is authorized.
    """

    transition_at = _now(now)
    submitted_at = _future(submitted_at, field_name='submitted_at')
    predecessor_run_id = _provider_predecessor_run_id(
        provider_predecessor_run_id,
    )
    try:
        account = _lock_account(claim.account_id)
    except FeedAccountUnavailable:
        return None
    run = MarketplaceFeedRun.objects.select_for_update().filter(
        pk=claim.run_id,
        account_id=claim.account_id,
        tenant_id=claim.tenant_id,
        marketplace=claim.marketplace,
        account_identity_digest=claim.account_identity_digest,
        state=MarketplaceFeedRun.State.PREPARING,
        revision=claim.revision,
        claim_token=claim.claim_token,
        claimed_until=claim.claimed_until,
        claimed_until__gt=transition_at,
        submitted_at__isnull=True,
        provider_run_id__isnull=True,
        provider_predecessor_run_id__isnull=True,
    ).first()
    if run is None:
        return None

    if not _live(account):
        _fence_invalid_owner_locked(
            run,
            safe_state=MarketplaceFeedRun.State.CANCELLED,
            now=transition_at,
            error='Marketplace account became inactive before feed submission.',
        )
        return None
    current_identity = account_identity_digest(account)
    if (
        run.tenant_id != account.tenant_id
        or run.marketplace != account.marketplace
        or run.account_identity_digest != current_identity
    ):
        _fence_invalid_owner_locked(
            run,
            safe_state=MarketplaceFeedRun.State.SUPERSEDED,
            now=transition_at,
            error='Marketplace account identity changed before feed submission.',
        )
        return None

    changed = MarketplaceFeedRun.objects.filter(
        pk=run.pk,
        account_id=account.pk,
        tenant_id=account.tenant_id,
        marketplace=account.marketplace,
        account_identity_digest=current_identity,
        state=MarketplaceFeedRun.State.PREPARING,
        revision=claim.revision,
        claim_token=claim.claim_token,
        claimed_until=claim.claimed_until,
        claimed_until__gt=transition_at,
        submitted_at__isnull=True,
        provider_run_id__isnull=True,
        provider_predecessor_run_id__isnull=True,
    ).update(
        state=MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
        submitted_at=submitted_at,
        provider_predecessor_run_id=predecessor_run_id,
        provider_result_deadline_at=submitted_at + PROVIDER_RESULT_HORIZON,
        submission_reconcile_attempt=0,
        revision=F('revision') + 1,
        last_error='Provider submission is in progress.',
        updated_at=transition_at,
    )
    if changed != 1:
        return None
    run.refresh_from_db()
    return _claim_snapshot(run)


@transaction.atomic
def validate_feed_submission_owner(
    claim: FeedRunClaim,
    *,
    now: datetime | None = None,
) -> bool:
    """Fence an invalid PREPARING owner before any provider baseline read.

    This is a local-only preflight.  The submission boundary repeats the same
    locked checks after the provider read, so an owner change in either gap
    still cannot authorize POST.
    """

    transition_at = _now(now)
    try:
        account = _lock_account(claim.account_id)
    except FeedAccountUnavailable:
        return False
    run = MarketplaceFeedRun.objects.select_for_update().filter(
        pk=claim.run_id,
        account_id=claim.account_id,
        tenant_id=claim.tenant_id,
        marketplace=claim.marketplace,
        account_identity_digest=claim.account_identity_digest,
        state=MarketplaceFeedRun.State.PREPARING,
        revision=claim.revision,
        claim_token=claim.claim_token,
        claimed_until=claim.claimed_until,
        claimed_until__gt=transition_at,
        submitted_at__isnull=True,
        provider_run_id__isnull=True,
        provider_predecessor_run_id__isnull=True,
    ).first()
    if run is None:
        return False
    if not _live(account):
        _fence_invalid_owner_locked(
            run,
            safe_state=MarketplaceFeedRun.State.CANCELLED,
            now=transition_at,
            error='Marketplace account became inactive before feed submission.',
        )
        return False
    current_identity = account_identity_digest(account)
    if (
        run.tenant_id != account.tenant_id
        or run.marketplace != account.marketplace
        or run.account_identity_digest != current_identity
    ):
        _fence_invalid_owner_locked(
            run,
            safe_state=MarketplaceFeedRun.State.SUPERSEDED,
            now=transition_at,
            error='Marketplace account identity changed before feed submission.',
        )
        return False
    return True


@transaction.atomic
def mark_feed_submitted(
    claim: FeedRunClaim,
    *,
    payload_sha256: str,
    provider_run_id: str | None,
    submitted_at: datetime,
    next_attempt_at: datetime,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Persist the exact provider upload identity before polling it."""

    transition_at = _now(now)
    payload_sha256 = _digest(payload_sha256, field_name='payload_sha256')
    submitted_at = _future(submitted_at, field_name='submitted_at')
    next_attempt_at = _future(next_attempt_at, field_name='next_attempt_at')
    provider_run_id = str(provider_run_id).strip() if provider_run_id is not None else None
    if provider_run_id == '' or (provider_run_id is not None and len(provider_run_id) > 200):
        raise ValueError('provider_run_id must be null or a non-empty string of at most 200 characters.')
    run = _lock_claimed_run(
        claim,
        allowed_states=(
            MarketplaceFeedRun.State.PREPARING,
            MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
            MarketplaceFeedRun.State.RETRY_WAIT,
        ),
        now=transition_at,
    )
    return _transition_claimed(
        run,
        claim,
        now=transition_at,
        updates={
            'state': MarketplaceFeedRun.State.POLLING,
            'payload_sha256': payload_sha256,
            'provider_run_id': provider_run_id,
            'submitted_at': submitted_at,
            'provider_result_deadline_at': (
                run.provider_result_deadline_at
                or submitted_at + PROVIDER_RESULT_HORIZON
            ),
            'submission_reconcile_attempt': 0,
            'next_attempt_at': next_attempt_at,
            'last_error': '',
        },
    )


@transaction.atomic
def mark_feed_submission_unknown(
    claim: FeedRunClaim,
    *,
    submitted_at: datetime,
    next_attempt_at: datetime,
    error: object,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    transition_at = _now(now)
    submitted_at = _future(submitted_at, field_name='submitted_at')
    next_attempt_at = _future(next_attempt_at, field_name='next_attempt_at', after=transition_at)
    run = _lock_claimed_run(
        claim,
        allowed_states=(MarketplaceFeedRun.State.PREPARING, MarketplaceFeedRun.State.SUBMIT_UNKNOWN),
        now=transition_at,
    )
    if run.submitted_at is not None and run.submitted_at != submitted_at:
        raise FeedRunConflict('Ambiguous submission timestamp does not match the feed generation.')
    return _transition_claimed(
        run,
        claim,
        now=transition_at,
        updates={
            'state': MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
            'submitted_at': submitted_at,
            'provider_result_deadline_at': (
                run.provider_result_deadline_at
                or submitted_at + PROVIDER_RESULT_HORIZON
            ),
            **(
                {'submission_reconcile_attempt': 0}
                if run.state == MarketplaceFeedRun.State.PREPARING
                else {}
            ),
            'next_attempt_at': next_attempt_at,
            'last_error': _error(error),
        },
    )


@transaction.atomic
def record_provider_run_observation(
    claim: FeedRunClaim,
    *,
    provider_run_id: str,
    next_attempt_at: datetime,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Bind a provider run observed after an ambiguous submission.

    A provider identity can be learned more than once, but it can never be
    replaced for the same generation.  The exact lease transition also lets a
    normal polling observation release its claim without moving the keyset
    cursor.
    """

    transition_at = _now(now)
    next_attempt_at = _future(next_attempt_at, field_name='next_attempt_at')
    observed_provider_run_id = _provider_run_id(provider_run_id)
    run = _lock_claimed_run(
        claim,
        allowed_states=(
            MarketplaceFeedRun.State.POLLING,
            MarketplaceFeedRun.State.SUBMIT_UNKNOWN,
        ),
        now=transition_at,
    )
    if run.submitted_at is None:
        raise FeedRunConflict(
            'A provider run cannot be bound without an ambiguous submission timestamp.',
        )
    if run.provider_run_id and run.provider_run_id != observed_provider_run_id:
        raise FeedRunConflict('Provider run identity does not match the feed generation.')
    if (
        not run.provider_run_id
        and run.provider_predecessor_run_id is not None
        and run.provider_predecessor_run_id == observed_provider_run_id
    ):
        raise FeedRunConflict('Provider observation is still the pre-POST predecessor.')
    return _transition_claimed(
        run,
        claim,
        now=transition_at,
        updates={
            'state': MarketplaceFeedRun.State.POLLING,
            'provider_run_id': observed_provider_run_id,
            'submission_reconcile_attempt': 0,
            'next_attempt_at': next_attempt_at,
            'last_error': '',
        },
    )


@transaction.atomic
def complete_poll_step(
    claim: FeedRunClaim,
    *,
    last_listing_id: int,
    published_delta: int = 0,
    rejected_delta: int = 0,
    other_resolved_delta: int = 0,
    next_attempt_at: datetime,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Advance one poll keyset page and atomically persist its counters."""

    transition_at = _now(now)
    next_attempt_at = _future(next_attempt_at, field_name='next_attempt_at')
    if isinstance(last_listing_id, bool) or not isinstance(last_listing_id, int):
        raise ValueError('last_listing_id must be an integer.')
    run = _lock_claimed_run(
        claim,
        allowed_states=(MarketplaceFeedRun.State.POLLING,),
        now=transition_at,
    )
    if last_listing_id <= run.poll_cursor_listing_id:
        raise FeedRunConflict('Poll cursor must advance monotonically.')
    counters = _counter_values(
        run,
        published_delta=published_delta,
        rejected_delta=rejected_delta,
        other_resolved_delta=other_resolved_delta,
    )
    return _transition_claimed(
        run,
        claim,
        now=transition_at,
        updates={
            **counters,
            'poll_cursor_listing_id': last_listing_id,
            'next_attempt_at': next_attempt_at,
            'last_error': '',
        },
    )


@transaction.atomic
def apply_poll_page(
    claim: FeedRunClaim,
    *,
    batch_listing_ids: Iterable[int],
    resolved_external_ids: Mapping[int, str],
    last_listing_id: int,
    next_attempt_at: datetime,
    occurred_at: datetime,
) -> FeedPageApplyResult:
    """Apply one bounded provider poll page and advance it atomically.

    The worker must return the exact deterministic page it loaded before the
    HTTP call.  Reordering, truncating, expanding, or replaying that page is a
    fenced conflict.  Listings, aggregate counters, audit logs, cursor, and
    lease are committed together.
    """

    transition_at = _now(occurred_at)
    next_attempt_at = _future(next_attempt_at, field_name='next_attempt_at')
    listing_ids = _poll_listing_ids(batch_listing_ids)
    if isinstance(last_listing_id, bool) or not isinstance(last_listing_id, int):
        raise ValueError('last_listing_id must be an integer.')
    if last_listing_id != listing_ids[-1]:
        raise ValueError('last_listing_id must be the final id in the exact poll batch.')
    external_ids = _resolved_external_ids(
        resolved_external_ids,
        batch_listing_ids=listing_ids,
    )

    run = _lock_claimed_run(
        claim,
        allowed_states=(MarketplaceFeedRun.State.POLLING,),
        now=transition_at,
    )
    if run.poll_cursor_listing_id != claim.poll_cursor_listing_id:
        raise StaleFeedRunClaim('Poll cursor changed after claim.')

    # The account and run are already locked.  Lock the deterministic current
    # page next, preserving the global account -> run -> listing order.
    listings = list(
        Listing.objects.select_for_update()
        .filter(
            feed_run_id=run.pk,
            account_id=run.account_id,
            tenant_id=run.tenant_id,
            status=Listing.STATUS_PENDING,
            external_id__isnull=True,
            pk__gt=run.poll_cursor_listing_id,
        )
        .order_by('pk')[:MAX_POLL_BATCH_SIZE]
    )
    if tuple(listing.pk for listing in listings) != listing_ids:
        raise FeedRunConflict('batch_listing_ids no longer matches the exact poll batch.')

    changed: list[Listing] = []
    due_at = transition_at + PUBLISHED_STATUS_RECHECK_DELAY
    for listing in listings:
        external_id = external_ids.get(listing.pk)
        if external_id is None:
            continue
        # Recheck every mutable predicate on the locked row.  These predicates
        # are intentionally redundant with the queryset and document the
        # exact generation/live fence at the mutation boundary.
        if (
            listing.feed_run_id != run.pk
            or listing.account_id != run.account_id
            or listing.tenant_id != run.tenant_id
            or listing.deleted_at is not None
            or listing.status != Listing.STATUS_PENDING
            or listing.external_id is not None
        ):
            continue
        listing.external_id = external_id
        listing.status = Listing.STATUS_ACTIVE
        listing.rejection_reason = ''
        if listing.published_at is None:
            listing.published_at = transition_at
        listing.last_sync_at = transition_at
        listing.remote_status = None
        listing.remote_status_checked_at = None
        listing.next_status_check_at = due_at
        listing.status_check_claim_token = None
        listing.status_check_claimed_until = None
        listing.updated_at = transition_at
        changed.append(listing)

    if changed:
        Listing.objects.bulk_update(changed, [
            'external_id',
            'status',
            'rejection_reason',
            'published_at',
            'last_sync_at',
            'remote_status',
            'remote_status_checked_at',
            'next_status_check_at',
            'status_check_claim_token',
            'status_check_claimed_until',
            'updated_at',
        ])
        SyncLog.objects.bulk_create([
            SyncLog(
                tenant_id=listing.tenant_id,
                product_id=listing.product_id,
                listing_id=listing.pk,
                event_type=SyncLog.EVENT_LISTING_PUBLISH,
                status=SyncLog.STATUS_OK,
                message='Listing published by marketplace feed.',
                payload={
                    'feed_generation_id': str(run.pk),
                    'provider_listing_id': listing.external_id,
                },
            )
            for listing in changed
        ])

    counters = _counter_values(
        run,
        published_delta=len(changed),
        rejected_delta=0,
        other_resolved_delta=0,
    )
    snapshot = _transition_claimed(
        run,
        claim,
        now=transition_at,
        updates={
            **counters,
            'poll_cursor_listing_id': last_listing_id,
            'next_attempt_at': next_attempt_at,
            'last_error': '',
        },
    )
    if changed:
        MarketplaceAccount.all_objects.filter(
            pk=run.account_id,
        ).filter(
            Q(status_batch_due_at__isnull=True)
            | Q(status_batch_due_at__gt=due_at),
        ).update(status_batch_due_at=due_at)
    changed_ids = tuple(listing.pk for listing in changed)
    return FeedPageApplyResult(
        snapshot=snapshot,
        changed_listing_ids=changed_ids,
        published_count=len(changed_ids),
    )


@transaction.atomic
def retry_step(
    claim: FeedRunClaim,
    *,
    next_attempt_at: datetime,
    error: object,
    increment_report_attempt: bool = False,
    increment_submission_attempt: bool = False,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Release a failed step without moving either keyset cursor."""

    transition_at = _now(now)
    next_attempt_at = _future(next_attempt_at, field_name='next_attempt_at', after=transition_at)
    run = _lock_claimed_run(
        claim,
        allowed_states=MarketplaceFeedRun.ACTIVE_STATES,
        now=transition_at,
    )
    updates: dict[str, object] = {
        'next_attempt_at': next_attempt_at,
        'last_error': _error(error),
    }
    if increment_report_attempt:
        if run.state != MarketplaceFeedRun.State.REPORTING:
            raise FeedRunConflict('Only a reporting step has a report attempt counter.')
        updates['report_attempt'] = run.report_attempt + 1
    if increment_submission_attempt:
        if run.state != MarketplaceFeedRun.State.SUBMIT_UNKNOWN:
            raise FeedRunConflict(
                'Only an ambiguous submission has a reconciliation attempt counter.',
            )
        updates['submission_reconcile_attempt'] = run.submission_reconcile_attempt + 1
    return _transition_claimed(run, claim, now=transition_at, updates=updates)


@transaction.atomic
def reset_poll_round(
    claim: FeedRunClaim,
    *,
    next_attempt_at: datetime,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Reset the keyset cursor only after the current round reached its tail."""

    transition_at = _now(now)
    next_attempt_at = _future(next_attempt_at, field_name='next_attempt_at', after=transition_at)
    run = _lock_claimed_run(
        claim,
        allowed_states=(MarketplaceFeedRun.State.POLLING,),
        now=transition_at,
    )
    has_more = Listing.objects.filter(
        feed_run_id=run.pk,
        account_id=run.account_id,
        status=Listing.STATUS_PENDING,
        external_id__isnull=True,
        pk__gt=run.poll_cursor_listing_id,
    ).exists()
    if has_more:
        raise FeedRunConflict('Poll round still has rows after its cursor.')
    pending_count = Listing.objects.filter(
        feed_run_id=run.pk,
        account_id=run.account_id,
        status=Listing.STATUS_PENDING,
        external_id__isnull=True,
    ).count()
    return _transition_claimed(
        run,
        claim,
        now=transition_at,
        updates={
            'poll_cursor_listing_id': 0,
            'poll_round': run.poll_round + 1,
            'pending_count': pending_count,
            'next_attempt_at': next_attempt_at,
            'last_error': '',
        },
    )


@transaction.atomic
def start_reporting(
    claim: FeedRunClaim,
    *,
    next_attempt_at: datetime,
    provider_run_id: str | None = None,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Move a terminal provider upload into exact-generation report paging."""

    transition_at = _now(now)
    next_attempt_at = _future(next_attempt_at, field_name='next_attempt_at')
    run = _lock_claimed_run(
        claim,
        allowed_states=(MarketplaceFeedRun.State.POLLING,),
        now=transition_at,
    )
    normalized_provider_id = str(provider_run_id).strip() if provider_run_id is not None else None
    if normalized_provider_id == '':
        raise ValueError('provider_run_id must not be blank.')
    if normalized_provider_id is not None and len(normalized_provider_id) > 200:
        raise ValueError('provider_run_id must be at most 200 characters.')
    if run.provider_run_id and normalized_provider_id and run.provider_run_id != normalized_provider_id:
        raise FeedRunConflict('Provider run identity does not match the submitted generation.')
    if run.report_completed_at is not None:
        raise FeedRunConflict('The exact provider report was already completed.')
    provider_identity = run.provider_run_id or normalized_provider_id
    if provider_identity is None:
        raise FeedRunConflict('Reporting requires an exact provider run identity.')
    return _transition_claimed(
        run,
        claim,
        now=transition_at,
        updates={
            'state': MarketplaceFeedRun.State.REPORTING,
            'provider_run_id': provider_identity,
            'report_page': 1,
            'report_attempt': 0,
            'next_attempt_at': next_attempt_at,
            'last_error': '',
        },
    )


@transaction.atomic
def advance_report_page(
    claim: FeedRunClaim,
    *,
    current_page: int,
    published_delta: int = 0,
    rejected_delta: int = 0,
    other_resolved_delta: int = 0,
    next_attempt_at: datetime,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Advance exactly one report page; delayed page results cannot skip ahead."""

    transition_at = _now(now)
    next_attempt_at = _future(next_attempt_at, field_name='next_attempt_at')
    if (
        isinstance(current_page, bool)
        or not isinstance(current_page, int)
        or not 1 <= current_page < MAX_REPORT_PAGES
    ):
        raise ValueError(
            f'current_page must be between 1 and {MAX_REPORT_PAGES - 1} when advancing.',
        )
    run = _lock_claimed_run(
        claim,
        allowed_states=(MarketplaceFeedRun.State.REPORTING,),
        now=transition_at,
    )
    if current_page != run.report_page or current_page != claim.report_page:
        raise StaleFeedRunClaim('Report page changed after claim.')
    counters = _counter_values(
        run,
        published_delta=published_delta,
        rejected_delta=rejected_delta,
        other_resolved_delta=other_resolved_delta,
    )
    return _transition_claimed(
        run,
        claim,
        now=transition_at,
        updates={
            **counters,
            'report_page': current_page + 1,
            'report_attempt': 0,
            'next_attempt_at': next_attempt_at,
            'last_error': '',
        },
    )


@transaction.atomic
def apply_report_page(
    claim: FeedRunClaim,
    *,
    current_page: int,
    errors_by_ad_id: Mapping[Any, object],
    external_ids_by_ad_id: Mapping[Any, object] | None = None,
    next_page: int | None,
    next_attempt_at: datetime | None,
    occurred_at: datetime,
    provider_complete: bool = True,
) -> FeedPageApplyResult:
    """Apply one exact-generation current-upload page atomically."""

    transition_at = _now(occurred_at)
    if (
        isinstance(current_page, bool)
        or not isinstance(current_page, int)
        or not 1 <= current_page <= MAX_REPORT_PAGES
    ):
        raise ValueError(f'current_page must be between 1 and {MAX_REPORT_PAGES}.')
    if next_page is not None:
        if isinstance(next_page, bool) or not isinstance(next_page, int):
            raise ValueError('next_page must be an integer or None.')
        if next_page != current_page + 1:
            raise ValueError('next_page must advance exactly one report page.')
        if next_page > MAX_REPORT_PAGES:
            raise ValueError(f'next_page must not exceed {MAX_REPORT_PAGES}.')
        if next_attempt_at is None:
            raise ValueError('next_attempt_at is required when another report page exists.')
    if next_attempt_at is not None:
        next_attempt_at = _future(next_attempt_at, field_name='next_attempt_at')
    if not isinstance(provider_complete, bool):
        raise ValueError('provider_complete must be a boolean.')
    if not provider_complete and next_page is None and next_attempt_at is None:
        raise ValueError(
            'next_attempt_at is required while the provider upload is incomplete.',
        )
    errors = _normalized_report_errors(errors_by_ad_id)
    external_ids = _normalized_report_external_ids(
        {} if external_ids_by_ad_id is None else external_ids_by_ad_id,
    )
    if set(errors).intersection(external_ids):
        raise ValueError(
            'One report item cannot be both rejected and published.',
        )
    outcome_ad_ids = tuple(set(errors).union(external_ids))
    if len(outcome_ad_ids) > MAX_POLL_BATCH_SIZE:
        raise ValueError(
            f'One report page must contain at most {MAX_POLL_BATCH_SIZE} outcomes.',
        )

    run = _lock_claimed_run(
        claim,
        allowed_states=(MarketplaceFeedRun.State.REPORTING,),
        now=transition_at,
    )
    if current_page != run.report_page or current_page != claim.report_page:
        raise StaleFeedRunClaim('Report page changed after claim.')

    listings = list(
        Listing.all_objects.select_for_update()
        .filter(
            feed_run_id=run.pk,
            account_id=run.account_id,
            tenant_id=run.tenant_id,
            deleted_at__isnull=True,
            status=Listing.STATUS_PENDING,
            external_id__isnull=True,
            publish_idempotency_key__in=outcome_ad_ids,
        )
        .order_by('pk')[:MAX_POLL_BATCH_SIZE]
    )
    changed: list[Listing] = []
    published: list[Listing] = []
    rejected: list[Listing] = []
    due_at = transition_at + PUBLISHED_STATUS_RECHECK_DELAY
    for listing in listings:
        if (
            listing.feed_run_id != run.pk
            or listing.account_id != run.account_id
            or listing.tenant_id != run.tenant_id
            or listing.deleted_at is not None
            or listing.status != Listing.STATUS_PENDING
            or listing.external_id is not None
        ):
            continue
        ad_id = listing.publish_idempotency_key
        rejection_reason = errors.get(ad_id)
        external_id = external_ids.get(ad_id)
        if rejection_reason is not None:
            listing.status = Listing.STATUS_REJECTED
            listing.rejection_reason = rejection_reason
            listing.next_status_check_at = None
            rejected.append(listing)
        elif external_id is not None:
            listing.external_id = external_id
            listing.status = Listing.STATUS_ACTIVE
            listing.rejection_reason = ''
            if listing.published_at is None:
                listing.published_at = transition_at
            listing.next_status_check_at = due_at
            published.append(listing)
        else:
            continue
        listing.last_sync_at = transition_at
        listing.remote_status = None
        listing.remote_status_checked_at = None
        listing.status_check_claim_token = None
        listing.status_check_claimed_until = None
        listing.updated_at = transition_at
        changed.append(listing)

    if changed:
        Listing.all_objects.bulk_update(changed, [
            'external_id',
            'status',
            'rejection_reason',
            'published_at',
            'last_sync_at',
            'remote_status',
            'remote_status_checked_at',
            'next_status_check_at',
            'status_check_claim_token',
            'status_check_claimed_until',
            'updated_at',
        ])
        SyncLog.objects.bulk_create([
            SyncLog(
                tenant_id=listing.tenant_id,
                product_id=listing.product_id,
                listing_id=listing.pk,
                event_type=SyncLog.EVENT_LISTING_ERROR,
                status=SyncLog.STATUS_ERROR,
                message=listing.rejection_reason,
                payload={'feed_generation_id': str(run.pk)},
            )
            for listing in rejected
        ] + [
            SyncLog(
                tenant_id=listing.tenant_id,
                product_id=listing.product_id,
                listing_id=listing.pk,
                event_type=SyncLog.EVENT_LISTING_PUBLISH,
                status=SyncLog.STATUS_OK,
                message='Listing published by marketplace feed.',
                payload={
                    'feed_generation_id': str(run.pk),
                    'provider_listing_id': listing.external_id,
                },
            )
            for listing in published
        ])

    if next_page is None:
        counters = _recomputed_counters(run)
        if counters['pending_count']:
            if provider_complete:
                # A complete provider report is not proof that every listing
                # was resolved. Return unresolved rows to the bounded ID poll.
                updates: dict[str, object] = {
                    **counters,
                    'state': MarketplaceFeedRun.State.POLLING,
                    'poll_cursor_listing_id': 0,
                    'poll_round': run.poll_round + 1,
                    'report_page': 1,
                    'report_attempt': 0,
                    'report_completed_at': transition_at,
                    'next_attempt_at': transition_at + UNRESOLVED_POLL_RETRY_DELAY,
                    'last_error': '',
                    'finished_at': None,
                }
            else:
                # ``current/items`` can expose proven rows while the upload is
                # still processing. Re-scan it later so unresolved rows can
                # receive their eventual error or active id.
                updates = {
                    **counters,
                    'state': MarketplaceFeedRun.State.REPORTING,
                    'report_page': 1,
                    'report_attempt': 0,
                    'report_completed_at': None,
                    'next_attempt_at': next_attempt_at,
                    'last_error': '',
                    'finished_at': None,
                }
        else:
            updates = {
                **counters,
                'state': MarketplaceFeedRun.State.SUCCEEDED,
                'next_attempt_at': None,
                'report_attempt': 0,
                'report_completed_at': transition_at,
                'last_error': '',
                'finished_at': transition_at,
            }
    else:
        updates = {
            **_counter_values(
                run,
                published_delta=len(published),
                rejected_delta=len(rejected),
                other_resolved_delta=0,
            ),
            'report_page': next_page,
            'report_attempt': 0,
            'next_attempt_at': next_attempt_at,
            'last_error': '',
        }
    snapshot = _transition_claimed(
        run,
        claim,
        now=transition_at,
        updates=updates,
    )
    if published:
        MarketplaceAccount.all_objects.filter(
            pk=run.account_id,
        ).filter(
            Q(status_batch_due_at__isnull=True)
            | Q(status_batch_due_at__gt=due_at),
        ).update(status_batch_due_at=due_at)
    changed_ids = tuple(listing.pk for listing in changed)
    return FeedPageApplyResult(
        snapshot=snapshot,
        changed_listing_ids=changed_ids,
        published_count=len(published),
        rejected_count=len(rejected),
    )


retry_report_page = retry_step


@transaction.atomic
def finish_feed_run(
    claim: FeedRunClaim,
    *,
    state: str = MarketplaceFeedRun.State.SUCCEEDED,
    error: object = '',
    increment_submission_attempt: bool = False,
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Finish an owned generation and revoke its lease in the same CAS."""

    transition_at = _now(now)
    if state not in (
        MarketplaceFeedRun.State.SUCCEEDED,
        MarketplaceFeedRun.State.FAILED,
        MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
        MarketplaceFeedRun.State.CANCELLED,
    ):
        raise ValueError(
            'finish state must be succeeded, failed, outcome_uncertain, or cancelled.',
        )
    run = _lock_claimed_run(
        claim,
        allowed_states=MarketplaceFeedRun.ACTIVE_STATES,
        now=transition_at,
    )
    if increment_submission_attempt and run.state != MarketplaceFeedRun.State.SUBMIT_UNKNOWN:
        raise FeedRunConflict(
            'Only an ambiguous submission has a reconciliation attempt counter.',
        )
    counters = _recomputed_counters(run)
    if state == MarketplaceFeedRun.State.SUCCEEDED:
        if counters['pending_count']:
            raise FeedRunConflict('A feed run with pending listings cannot succeed.')
        if run.submitted_at is not None and run.report_completed_at is None:
            raise FeedRunConflict(
                'A submitted feed run cannot succeed before its exact report completes.',
            )
    submission_updates = (
        {'submission_reconcile_attempt': run.submission_reconcile_attempt + 1}
        if increment_submission_attempt
        else {}
    )
    return _transition_claimed(
        run,
        claim,
        now=transition_at,
        updates={
            **counters,
            **submission_updates,
            'state': state,
            'next_attempt_at': None,
            'last_error': _error(error),
            'finished_at': transition_at,
        },
    )


@transaction.atomic
def reconcile_uncertain_feed_run(
    run_id: uuid.UUID,
    expected_revision: int,
    resolution: str,
    provider_run_id: str | None = None,
    now: datetime | None = None,
    *,
    allow_tombstone: bool = False,
) -> FeedRunSnapshot:
    """Resolve one fail-closed submission after independent operator review.

    This is deliberately not an automatic recovery path.  It requires the
    exact terminal revision, locks account before run, and revalidates the
    provider identity.  Direct callers remain live-owner-only by default.
    An explicit tombstone option may close a rejected submission for an
    inactive/soft-deleted owner without restoring it. Neither path changes the
    generation's listing membership or provider identities.
    """

    transition_at = _now(now)
    try:
        generation_id = (
            run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
        )
    except (AttributeError, TypeError, ValueError):
        raise ValueError('run_id must be a UUID.') from None
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError('expected_revision must be a non-negative integer.')
    if resolution not in FEED_RUN_RECONCILIATION_RESOLUTIONS:
        raise ValueError(
            'resolution must be provider_accepted or provider_not_accepted.',
        )
    if not isinstance(allow_tombstone, bool):
        raise ValueError('allow_tombstone must be a boolean.')
    if allow_tombstone and resolution != RECONCILIATION_PROVIDER_NOT_ACCEPTED:
        raise ValueError(
            'allow_tombstone is allowed only for provider_not_accepted.',
        )
    if resolution == RECONCILIATION_PROVIDER_ACCEPTED:
        normalized_provider_run_id = _provider_run_id(provider_run_id)
    else:
        if provider_run_id is not None:
            raise ValueError(
                'provider_run_id is allowed only for provider_accepted.',
            )
        normalized_provider_run_id = None

    account_id = MarketplaceFeedRun.objects.filter(pk=generation_id).values_list(
        'account_id', flat=True,
    ).first()
    if account_id is None:
        raise FeedRunConflict('Feed run does not exist.')
    account = _lock_account(account_id)
    inactive_owner = not account.is_active
    if not account.tenant.is_active:
        raise FeedAccountUnavailable(
            f'Marketplace account {account_id} belongs to an inactive tenant.',
        )
    unavailable_owner_allowed = (
        inactive_owner
        and allow_tombstone
        and resolution == RECONCILIATION_PROVIDER_NOT_ACCEPTED
    )
    if not _live(account) and not unavailable_owner_allowed:
        raise FeedAccountUnavailable(
            f'Marketplace account {account_id} is inactive or deleted.',
        )

    run = MarketplaceFeedRun.objects.select_for_update().filter(
        pk=generation_id,
        account_id=account.pk,
    ).first()
    if run is None:
        raise FeedRunConflict('Feed run does not exist for the locked account.')
    if run.state != MarketplaceFeedRun.State.OUTCOME_UNCERTAIN:
        raise FeedRunConflict('Feed run is not awaiting operator reconciliation.')
    if run.revision != expected_revision:
        raise FeedRunConflict('Feed run revision changed.')
    current_identity = account_identity_digest(account)
    if (
        run.tenant_id != account.tenant_id
        or run.marketplace != account.marketplace
        or run.account_identity_digest != current_identity
    ):
        raise FeedRunConflict('Marketplace account identity changed.')
    if run.submitted_at is None:
        raise FeedRunConflict(
            'An uncertain feed run without a submission timestamp cannot be reconciled.',
        )

    updates: dict[str, object] = {
        'revision': F('revision') + 1,
        'submission_reconcile_attempt': 0,
        'report_attempt': 0,
        'claim_token': None,
        'claimed_until': None,
        'updated_at': transition_at,
    }
    if resolution == RECONCILIATION_PROVIDER_ACCEPTED:
        if run.provider_run_id and run.provider_run_id != normalized_provider_run_id:
            raise FeedRunConflict(
                'Provider run identity does not match the feed generation.',
            )
        if MarketplaceFeedRun.objects.filter(
            account_id=account.pk,
            provider_run_id=normalized_provider_run_id,
        ).exclude(pk=run.pk).exists():
            raise FeedRunConflict(
                'Provider run identity already belongs to another feed generation.',
            )
        if MarketplaceFeedRun.objects.filter(
            account_id=account.pk,
            state__in=MarketplaceFeedRun.ACTIVE_STATES,
        ).exclude(pk=run.pk).exists():
            raise FeedRunConflict(
                'Another active feed generation already owns this account.',
            )
        updates.update({
            'state': MarketplaceFeedRun.State.POLLING,
            'provider_run_id': normalized_provider_run_id,
            'provider_result_deadline_at': transition_at + PROVIDER_RESULT_HORIZON,
            'report_completed_at': None,
            'next_attempt_at': transition_at,
            'finished_at': None,
            'last_error': 'Operator reconciled: provider accepted the submission.',
        })
    else:
        updates.update({
            'state': MarketplaceFeedRun.State.FAILED,
            'next_attempt_at': None,
            'finished_at': transition_at,
            'last_error': 'Operator reconciled: provider did not accept the submission.',
        })

    changed = MarketplaceFeedRun.objects.filter(
        pk=run.pk,
        account_id=account.pk,
        tenant_id=account.tenant_id,
        marketplace=account.marketplace,
        account_identity_digest=current_identity,
        state=MarketplaceFeedRun.State.OUTCOME_UNCERTAIN,
        revision=expected_revision,
        submitted_at=run.submitted_at,
        account__tenant__is_active=True,
    ).update(**updates)
    if changed != 1:
        raise FeedRunConflict('Feed run changed during operator reconciliation.')
    run.refresh_from_db()
    return _snapshot(run)


@transaction.atomic
def supersede_feed_run(
    run_id: uuid.UUID,
    *,
    expected_revision: int | None = None,
    reason: object = '',
    now: datetime | None = None,
) -> FeedRunSnapshot:
    """Administratively fence an active generation without a worker claim."""

    transition_at = _now(now)
    identity = MarketplaceFeedRun.objects.filter(pk=run_id).values('account_id').first()
    if identity is None:
        raise FeedRunConflict('Feed run does not exist.')
    _lock_account(identity['account_id'])
    run = MarketplaceFeedRun.objects.select_for_update().get(pk=run_id)
    if run.state in MarketplaceFeedRun.TERMINAL_STATES:
        if expected_revision is not None and run.revision != expected_revision:
            raise FeedRunConflict('Feed run revision changed.')
        return _snapshot(run)
    if expected_revision is not None and run.revision != expected_revision:
        raise FeedRunConflict('Feed run revision changed.')
    return _fence_invalid_owner_locked(
        run,
        safe_state=MarketplaceFeedRun.State.SUPERSEDED,
        now=transition_at,
        error=reason,
    )
