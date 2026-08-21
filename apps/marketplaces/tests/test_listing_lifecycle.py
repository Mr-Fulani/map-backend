from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from apps.marketplaces.listing_lifecycle import (
    ListingLifecycleUpdate,
    claim_status_check,
    clear_remote_observation,
    complete_claimed_status_check,
    normalize_remote_status,
    record_remote_observation,
    release_status_check,
    schedule_status_check,
)
from apps.marketplaces.models import Listing


NOW = datetime(2026, 8, 13, 18, 0, tzinfo=timezone.utc)
NEXT_CHECK = NOW + timedelta(hours=24)
LEASE_UNTIL = NOW + timedelta(minutes=15)


@pytest.mark.parametrize(
    ('raw_status', 'expected'),
    [
        ('active', Listing.REMOTE_STATUS_ACTIVE),
        (' ACTIVE ', Listing.REMOTE_STATUS_ACTIVE),
        ('Rejected', Listing.REMOTE_STATUS_REJECTED),
        ('blocked', Listing.REMOTE_STATUS_BLOCKED),
        ('removed', Listing.REMOTE_STATUS_REMOVED),
        ('archived', Listing.REMOTE_STATUS_ARCHIVED),
        ('new-provider-state', Listing.REMOTE_STATUS_OTHER),
        ('', Listing.REMOTE_STATUS_OTHER),
        (None, Listing.REMOTE_STATUS_OTHER),
        (17, Listing.REMOTE_STATUS_OTHER),
    ],
)
def test_normalize_remote_status_uses_bounded_provider_neutral_vocabulary(raw_status, expected):
    assert normalize_remote_status(raw_status) == expected


def test_normalize_remote_status_accepts_explicit_provider_aliases():
    aliases = {
        'published': Listing.REMOTE_STATUS_ACTIVE,
        'moderation-failed': Listing.REMOTE_STATUS_REJECTED,
    }

    assert normalize_remote_status(' PUBLISHED ', aliases=aliases) == Listing.REMOTE_STATUS_ACTIVE
    assert normalize_remote_status('moderation-failed', aliases=aliases) == Listing.REMOTE_STATUS_REJECTED


@pytest.mark.parametrize(
    'aliases',
    [
        {'': Listing.REMOTE_STATUS_ACTIVE},
        {'published': 'provider-private-state'},
        {'PUBLISHED': Listing.REMOTE_STATUS_ACTIVE, ' published ': Listing.REMOTE_STATUS_REJECTED},
    ],
)
def test_normalize_remote_status_rejects_invalid_alias_contracts(aliases):
    with pytest.raises(ValueError):
        normalize_remote_status('published', aliases=aliases)


def test_update_can_drive_instance_save_without_mutating_canonical_status():
    listing = SimpleNamespace(
        status=Listing.STATUS_ACTIVE,
        remote_status=None,
        remote_status_checked_at=None,
        next_status_check_at=None,
    )
    update = record_remote_observation(
        'published',
        checked_at=NOW,
        next_status_check_at=NEXT_CHECK,
        aliases={'published': Listing.REMOTE_STATUS_ACTIVE},
    )

    update_fields = update.apply_to(listing)

    assert update_fields == (
        'remote_status',
        'remote_status_checked_at',
        'next_status_check_at',
    )
    assert listing.status == Listing.STATUS_ACTIVE
    assert listing.remote_status == Listing.REMOTE_STATUS_ACTIVE
    assert listing.remote_status_checked_at == NOW
    assert listing.next_status_check_at == NEXT_CHECK
    assert 'status' not in update.as_update_kwargs()
    assert 'status_check_claim_token' not in update.as_update_kwargs()


def test_schedule_claim_release_and_clear_build_minimal_updates():
    token = uuid4()

    assert schedule_status_check(NEXT_CHECK).as_update_kwargs() == {
        'next_status_check_at': NEXT_CHECK,
    }
    assert schedule_status_check(None).as_update_kwargs() == {
        'next_status_check_at': None,
    }
    assert claim_status_check(
        claim_token=str(token),
        claimed_until=LEASE_UNTIL,
    ).as_update_kwargs() == {
        'status_check_claim_token': token,
        'status_check_claimed_until': LEASE_UNTIL,
    }
    assert release_status_check(next_status_check_at=NEXT_CHECK).as_update_kwargs() == {
        'next_status_check_at': NEXT_CHECK,
        'status_check_claim_token': None,
        'status_check_claimed_until': None,
    }
    assert clear_remote_observation().as_update_kwargs() == {
        'remote_status': None,
        'remote_status_checked_at': None,
    }


def test_claimed_completion_records_observation_and_explicitly_clears_claim():
    update = complete_claimed_status_check(
        'blocked',
        checked_at=NOW,
        next_status_check_at=NEXT_CHECK,
    )

    assert update.as_update_kwargs() == {
        'remote_status': Listing.REMOTE_STATUS_BLOCKED,
        'remote_status_checked_at': NOW,
        'next_status_check_at': NEXT_CHECK,
        'status_check_claim_token': None,
        'status_check_claimed_until': None,
    }
    assert 'status' not in update.update_fields


@pytest.mark.parametrize(
    'factory',
    [
        lambda: schedule_status_check(datetime(2026, 8, 13, 18, 0)),
        lambda: claim_status_check(claim_token=uuid4(), claimed_until=datetime(2026, 8, 13, 18, 15)),
        lambda: release_status_check(next_status_check_at=datetime(2026, 8, 14, 18, 0)),
        lambda: record_remote_observation(
            'active',
            checked_at=datetime(2026, 8, 13, 18, 0),
            next_status_check_at=NEXT_CHECK,
        ),
        lambda: record_remote_observation(
            'active',
            checked_at=NOW,
            next_status_check_at=datetime(2026, 8, 14, 18, 0),
        ),
    ],
)
def test_lifecycle_datetimes_must_be_timezone_aware(factory):
    with pytest.raises(ValueError, match='timezone-aware'):
        factory()


@pytest.mark.parametrize('token', [None, '', 'not-a-uuid', 17])
def test_claim_token_must_be_a_uuid(token):
    with pytest.raises(ValueError, match='claim_token'):
        claim_status_check(claim_token=token, claimed_until=LEASE_UNTIL)


def test_update_rejects_fields_outside_remote_lifecycle_contract():
    with pytest.raises(ValueError, match='Unsupported lifecycle fields'):
        ListingLifecycleUpdate((('status', Listing.STATUS_REJECTED),))
