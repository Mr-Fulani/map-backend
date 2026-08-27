import base64
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db import transaction
from django.test import Client, override_settings
from django.utils import timezone

from apps.datasources.encryption import encrypt
from apps.marketplaces.feed_endpoint import (
    FeedEndpointConfigurationError,
    canonical_marketplace_feed_cdn_origin,
    marketplace_feed_capability,
    marketplace_feed_public_url,
    parse_marketplace_feed_url_signing_keys,
    verify_marketplace_feed_capability,
)
from apps.marketplaces.feed_workflow import account_identity_digest
from apps.marketplaces.models import MarketplaceAccount, MarketplaceFeedEndpoint
from apps.marketplaces.services import MarketplaceAccountService
from apps.marketplaces.services import MarketplaceAccountFeedConflict
from apps.tenants.models import Tenant


SIGNING_KEY = b'feed-route-test-key-material--32-bytes'
ROUTE_SETTINGS = {
    'MARKETPLACE_FEED_STORAGE_MODE': 'stable_bridge',
    'MARKETPLACE_FEED_PUBLIC_BASE_URL': (
        'https://feeds.example.test/marketplace-feeds/v1/feed.xml'
    ),
    'MARKETPLACE_FEED_URL_SIGNING_KEYS': {'route-v1': SIGNING_KEY},
    'MARKETPLACE_FEED_URL_SIGNING_PRIMARY_KEY_ID': 'route-v1',
    'MEDIA_KEY_PREFIX': 'dev',
    'YC_S3_BUCKET': 'feed-bucket',
    'YC_CDN_DOMAIN': '',
}


def _account(slug='feed-route'):
    tenant = Tenant.objects.create(name='Feed route tenant', slug=slug)
    account = MarketplaceAccount.objects.create(
        tenant=tenant,
        marketplace=MarketplaceAccount.MARKETPLACE_AVITO,
        name='Route account',
        external_id=f'{slug}-external',
        credentials_enc=b'opaque-test-credentials',
    )
    return tenant, account


def _endpoint(account, **values):
    object_key = (
        f'dev/feeds/{account.tenant.slug}/{account.marketplace}/'
        f'route-account-{account.pk}/feed.xml'
    )
    defaults = {
        'token_key_id': 'route-v1',
        'owner_identity_digest': account_identity_digest(account),
        'serve_enabled': True,
        'storage_mode': MarketplaceFeedEndpoint.StorageMode.LEGACY_BRIDGE,
        'legacy_object_key': object_key,
        'legacy_profile_url': (
            f'https://storage.yandexcloud.net/feed-bucket/{object_key}'
        ),
        'profile_state': MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY,
        'profile_fingerprint': 'a' * 64,
        'profile_verified_at': timezone.now(),
    }
    defaults.update(values)
    return MarketplaceFeedEndpoint.objects.create(account=account, **defaults)


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_valid_capability_redirects_get_and_head_to_exact_frozen_legacy_url():
    _tenant, account = _account()
    endpoint = _endpoint(account)
    public_url = marketplace_feed_public_url(endpoint)
    parsed = urlsplit(public_url)
    query = parse_qs(parsed.query)

    assert parsed.path == '/marketplace-feeds/v1/feed.xml'
    assert query == {
        'id': [str(endpoint.public_id)],
        'key': [marketplace_feed_capability(endpoint)],
    }
    client = Client()
    for method in (client.get, client.head):
        response = method(f'{parsed.path}?{parsed.query}')

        assert response.status_code == 307
        assert response['Location'] == endpoint.legacy_profile_url
        assert response['Cache-Control'] == 'no-store, max-age=0'
        assert response['Referrer-Policy'] == 'no-referrer'
        assert response['X-Robots-Tag'] == 'noindex, nofollow, noarchive'
        assert 'Set-Cookie' not in response.headers


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_bridge_accepts_exact_historical_unprefixed_map_feed():
    _tenant, account = _account('historical-feed-route')
    key = (
        f'feeds/{account.tenant.slug}/{account.marketplace}/'
        f'route-account-{account.pk}/feed.xml'
    )
    endpoint = _endpoint(
        account,
        legacy_object_key=key,
        legacy_profile_url=(
            f'https://storage.yandexcloud.net/feed-bucket/{key}'
        ),
    )
    parsed = urlsplit(marketplace_feed_public_url(endpoint))

    response = Client().get(f'{parsed.path}?{parsed.query}')

    assert response.status_code == 307
    assert response['Location'] == endpoint.legacy_profile_url


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_capability_url_survives_account_and_tenant_rename():
    tenant, account = _account('feed-before-rename')
    endpoint = _endpoint(account)
    before = marketplace_feed_public_url(endpoint)

    tenant.slug = 'feed-after-rename'
    tenant.save(update_fields=['slug'])
    account.name = 'Renamed account'
    account.save(update_fields=['name'])
    endpoint.refresh_from_db()

    assert marketplace_feed_public_url(endpoint) == before


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_existing_endpoint_is_sticky_when_global_mode_rolls_back_to_legacy_public():
    _tenant, account = _account('sticky-legacy-public')
    endpoint = _endpoint(account)
    parsed = urlsplit(marketplace_feed_public_url(endpoint))

    with override_settings(MARKETPLACE_FEED_STORAGE_MODE='legacy_public'):
        response = Client().get(f'{parsed.path}?{parsed.query}')

    assert response.status_code == 307
    assert response['Location'] == endpoint.legacy_profile_url
    assert response['Cache-Control'] == 'no-store, max-age=0'


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
@pytest.mark.parametrize(
    'query',
    (
        '',
        '?id=not-a-uuid&key=' + ('a' * 43),
        '?id=00000000-0000-0000-0000-000000000000',
        '?id=00000000-0000-0000-0000-000000000000&key=short',
        '?id=00000000-0000-0000-0000-000000000000&key='
        + ('a' * 43) + '&extra=1',
        '?id=00000000-0000-0000-0000-000000000000&id='
        '00000000-0000-0000-0000-000000000000&key=' + ('a' * 43),
    ),
)
def test_malformed_capabilities_have_one_fail_closed_404_contract(query):
    response = Client().get(f'/marketplace-feeds/v1/feed.xml{query}')

    assert response.status_code == 404
    assert response.content == b''
    assert response['Referrer-Policy'] == 'no-referrer'


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_wrong_key_unservable_state_and_untrusted_target_all_fail_closed():
    _tenant, account = _account('feed-fail-closed')
    endpoint = _endpoint(account)
    public_url = marketplace_feed_public_url(endpoint)
    parsed = urlsplit(public_url)
    query = parse_qs(parsed.query)

    wrong_key_query = (
        f'?id={endpoint.public_id}&key=' + ('a' * 43)
    )
    assert Client().get(f'{parsed.path}{wrong_key_query}').status_code == 404

    endpoint.profile_state = MarketplaceFeedEndpoint.ProfileState.MANUAL_REVIEW
    endpoint.serve_enabled = False
    endpoint.save(update_fields=['profile_state', 'serve_enabled'])
    assert Client().get(f'{parsed.path}?{parsed.query}').status_code == 404

    endpoint.profile_state = MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
    endpoint.serve_enabled = True
    endpoint.legacy_profile_url = 'https://attacker.example/feed.xml'
    endpoint.save(update_fields=[
        'profile_state', 'serve_enabled', 'legacy_profile_url',
    ])
    assert Client().get(f'{parsed.path}?{parsed.query}').status_code == 404

    assert query['key'][0] not in repr(Client().get(f'{parsed.path}?{parsed.query}'))


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_disabled_or_inactive_ownership_never_serves_bridge():
    tenant, account = _account('feed-inactive')
    endpoint = _endpoint(account, serve_enabled=False)
    endpoint.serve_enabled = True
    public_url = marketplace_feed_public_url(endpoint)
    parsed = urlsplit(public_url)

    assert Client().get(f'{parsed.path}?{parsed.query}').status_code == 404

    MarketplaceFeedEndpoint.objects.filter(pk=endpoint.pk).update(serve_enabled=True)
    MarketplaceAccount.objects.filter(pk=account.pk).update(is_active=False)
    assert Client().get(f'{parsed.path}?{parsed.query}').status_code == 404

    MarketplaceAccount.objects.filter(pk=account.pk).update(is_active=True)
    Tenant.objects.filter(pk=tenant.pk).update(is_active=False)
    assert Client().get(f'{parsed.path}?{parsed.query}').status_code == 404


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_unsupported_method_is_405_without_redirect():
    _tenant, account = _account('feed-method')
    endpoint = _endpoint(account)
    parsed = urlsplit(marketplace_feed_public_url(endpoint))

    response = Client().post(
        f'{parsed.path}?{parsed.query}',
        data=b'ignored',
        content_type='text/plain',
    )

    assert response.status_code == 405
    assert set(response['Allow'].split(', ')) == {'GET', 'HEAD'}
    assert 'Location' not in response
    assert response['Cache-Control'] == 'no-store, max-age=0'


def test_hmac_is_domain_bound_and_parser_requires_strong_base64url_keys(settings):
    encoded = base64.urlsafe_b64encode(SIGNING_KEY).decode().rstrip('=')
    assert parse_marketplace_feed_url_signing_keys(
        f'{{"route-v1":"{encoded}"}}',
    ) == {'route-v1': SIGNING_KEY}
    with pytest.raises(ValueError, match='at least 32 bytes'):
        parse_marketplace_feed_url_signing_keys('{"weak":"c2hvcnQ"}')
    with pytest.raises(ValueError, match='duplicate key id'):
        parse_marketplace_feed_url_signing_keys(
            f'{{"route-v1":"{encoded}"," route-v1 ":"{encoded}"}}',
        )

    settings.MARKETPLACE_FEED_URL_SIGNING_KEYS = {'route-v1': SIGNING_KEY}
    first = SimpleNamespace(
        public_id='00000000-0000-0000-0000-000000000001',
        account_id=1,
        account=SimpleNamespace(tenant_id=2),
        token_key_id='route-v1',
        previous_token_key_id='',
        owner_identity_digest='a' * 64,
        capability_revision=1,
    )
    moved = SimpleNamespace(
        public_id=first.public_id,
        account_id=3,
        account=SimpleNamespace(tenant_id=2),
        token_key_id='route-v1',
        previous_token_key_id='',
        owner_identity_digest='a' * 64,
        capability_revision=1,
    )

    capability = marketplace_feed_capability(first)

    assert verify_marketplace_feed_capability(first, capability)
    assert not verify_marketplace_feed_capability(moved, capability)
    assert len(capability) == 43


@pytest.mark.django_db
@override_settings(
    **{
        **ROUTE_SETTINGS,
        'MARKETPLACE_FEED_URL_SIGNING_KEYS': {
            'route-v1': SIGNING_KEY,
            'route-v2': b'rotated-feed-route-key-material-32b',
        },
    },
)
def test_bounded_dual_key_rotation_accepts_previous_but_generates_current():
    _tenant, account = _account('feed-dual-key')
    endpoint = _endpoint(account)
    old_capability = marketplace_feed_capability(endpoint)

    endpoint.previous_token_key_id = 'route-v1'
    endpoint.token_key_id = 'route-v2'
    endpoint.profile_state = MarketplaceFeedEndpoint.ProfileState.MIGRATING
    endpoint.save(update_fields=(
        'previous_token_key_id', 'token_key_id', 'profile_state',
    ))
    current_capability = marketplace_feed_capability(endpoint)

    assert endpoint.capability_revision == 1
    assert old_capability != current_capability
    assert verify_marketplace_feed_capability(endpoint, old_capability)
    assert verify_marketplace_feed_capability(endpoint, current_capability)
    for capability in (old_capability, current_capability):
        response = Client().get(
            f'/marketplace-feeds/v1/feed.xml?id={endpoint.public_id}'
            f'&key={capability}',
        )
        assert response.status_code == 307


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_old_url_is_revoked_when_current_account_identity_changes():
    _tenant, account = _account('feed-owner-change')
    endpoint = _endpoint(account)
    parsed = urlsplit(marketplace_feed_public_url(endpoint))

    MarketplaceAccount.objects.filter(pk=account.pk).update(
        credentials_enc=b'a-new-provider-generation',
    )

    assert Client().get(f'{parsed.path}?{parsed.query}').status_code == 404


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_preprofile_credential_writer_rekeys_recoverable_new_endpoint():
    _tenant, account = _account('feed-service-fence')
    endpoint = _endpoint(
        account,
        profile_state=MarketplaceFeedEndpoint.ProfileState.NEW,
        serve_enabled=False,
        profile_fingerprint='b' * 64,
        profile_verified_at=timezone.now(),
        profile_revision=7,
    )
    parsed = urlsplit(marketplace_feed_public_url(endpoint))

    with patch.object(
        MarketplaceAccountService,
        '_fetch_avito_user_id',
        return_value=account.external_id,
    ):
        updated = MarketplaceAccountService.update_credentials(account, {
            'name': 'Rotated account',
            'marketplace': account.marketplace,
            'client_id': 'rotated-client',
            'client_secret': 'rotated-secret',
        })

    endpoint.refresh_from_db()
    assert endpoint.owner_identity_digest == account_identity_digest(updated)
    assert endpoint.capability_revision == 2
    assert endpoint.previous_token_key_id == ''
    assert endpoint.serve_enabled is False
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.NEW
    assert endpoint.profile_fingerprint == ''
    assert endpoint.profile_verified_at is None
    assert endpoint.profile_revision == 8
    assert Client().get(f'{parsed.path}?{parsed.query}').status_code == 404


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_identity_rollback_cannot_resurrect_an_old_capability():
    _tenant, account = _account('feed-identity-rollback')
    account.credentials_enc = encrypt({
        'client_id': 'original-client',
        'client_secret': 'original-secret',
    })
    account.save(update_fields=['credentials_enc'])
    original_identity = account_identity_digest(account)
    endpoint = _endpoint(
        account,
        profile_state=MarketplaceFeedEndpoint.ProfileState.NEW,
        serve_enabled=False,
    )
    old_url = urlsplit(marketplace_feed_public_url(endpoint))

    with patch.object(
        MarketplaceAccountService,
        '_fetch_avito_user_id',
        return_value=account.external_id,
    ):
        MarketplaceAccountService.update_credentials(account, {
            'name': 'Temporary identity',
            'marketplace': account.marketplace,
            'client_id': 'temporary-client',
            'client_secret': 'temporary-secret',
        })
        restored = MarketplaceAccountService.update_credentials(account, {
            'name': 'Original identity restored',
            'marketplace': account.marketplace,
            'client_id': 'original-client',
            'client_secret': 'original-secret',
        })

    assert account_identity_digest(restored) == original_identity
    endpoint.refresh_from_db()
    assert endpoint.owner_identity_digest == original_identity
    assert endpoint.capability_revision == 3
    endpoint.profile_state = MarketplaceFeedEndpoint.ProfileState.BRIDGE_READY
    endpoint.serve_enabled = True
    endpoint.profile_fingerprint = 'd' * 64
    endpoint.profile_verified_at = timezone.now()
    endpoint.save(update_fields=(
        'profile_state', 'serve_enabled', 'profile_fingerprint',
        'profile_verified_at',
    ))
    current_url = urlsplit(marketplace_feed_public_url(endpoint))

    assert Client().get(f'{old_url.path}?{old_url.query}').status_code == 404
    assert Client().get(f'{current_url.path}?{current_url.query}').status_code == 307


@pytest.mark.django_db(transaction=True)
@override_settings(**ROUTE_SETTINGS)
def test_preprofile_credential_writer_rollback_preserves_endpoint_generation():
    _tenant, account = _account('feed-service-rollback')
    endpoint = _endpoint(
        account,
        profile_state=MarketplaceFeedEndpoint.ProfileState.NEW,
        serve_enabled=False,
    )
    parsed = urlsplit(marketplace_feed_public_url(endpoint))
    original_owner = endpoint.owner_identity_digest

    with patch.object(
        MarketplaceAccountService,
        '_fetch_avito_user_id',
        return_value=account.external_id,
    ), pytest.raises(RuntimeError, match='force rollback'):
        with transaction.atomic():
            MarketplaceAccountService.update_credentials(account, {
                'name': 'Rolled back account',
                'marketplace': account.marketplace,
                'client_id': 'rolled-back-client',
                'client_secret': 'rolled-back-secret',
            })
            raise RuntimeError('force rollback')

    endpoint.refresh_from_db()
    account.refresh_from_db()
    assert endpoint.owner_identity_digest == original_owner
    assert endpoint.capability_revision == 1
    assert endpoint.serve_enabled is False
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.NEW
    assert Client().get(f'{parsed.path}?{parsed.query}').status_code == 404


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_name_only_service_update_keeps_endpoint_generation_stable():
    _tenant, account = _account('feed-name-stable')
    endpoint = _endpoint(account)
    before = marketplace_feed_public_url(endpoint)

    MarketplaceAccountService.update_partial(account, {'name': 'Display name only'})

    endpoint.refresh_from_db()
    assert marketplace_feed_public_url(endpoint) == before
    parsed = urlsplit(before)
    assert Client().get(f'{parsed.path}?{parsed.query}').status_code == 307


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_credential_restore_rekeys_unpublished_soft_deleted_endpoint():
    tenant, account = _account('feed-credential-restore')
    endpoint = _endpoint(
        account,
        profile_state=MarketplaceFeedEndpoint.ProfileState.NEW,
        serve_enabled=False,
    )
    before = urlsplit(marketplace_feed_public_url(endpoint))
    account.soft_delete()

    with patch.object(
        MarketplaceAccountService,
        '_fetch_avito_user_id',
        return_value=account.external_id,
    ):
        restored = MarketplaceAccountService.create(tenant, {
            'name': 'Restored with new credentials',
            'marketplace': account.marketplace,
            'client_id': 'restored-client',
            'client_secret': 'restored-secret',
        })

    endpoint.refresh_from_db()
    assert endpoint.owner_identity_digest == account_identity_digest(restored)
    assert endpoint.capability_revision == 2
    assert endpoint.serve_enabled is False
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.NEW
    assert Client().get(f'{before.path}?{before.query}').status_code == 404


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_verified_endpoint_allows_noop_credentials_but_blocks_real_rotation():
    _tenant, account = _account('feed-verified-credentials')
    account.credentials_enc = encrypt({
        'client_id': 'same-client',
        'client_secret': 'same-secret',
    })
    account.save(update_fields=['credentials_enc'])
    endpoint = _endpoint(
        account,
        profile_state=MarketplaceFeedEndpoint.ProfileState.VERIFIED,
        serve_enabled=True,
    )
    before = marketplace_feed_public_url(endpoint)

    with patch.object(
        MarketplaceAccountService,
        '_fetch_avito_user_id',
        return_value=account.external_id,
    ):
        updated = MarketplaceAccountService.update_credentials(account, {
            'name': 'Renamed only',
            'marketplace': account.marketplace,
            'client_id': 'same-client',
            'client_secret': 'same-secret',
        })
        with pytest.raises(MarketplaceAccountFeedConflict):
            MarketplaceAccountService.update_credentials(updated, {
                'name': 'Unsafe rotation',
                'marketplace': account.marketplace,
                'client_id': 'same-client',
                'client_secret': 'different-secret',
            })

    endpoint.refresh_from_db()
    assert endpoint.profile_state == MarketplaceFeedEndpoint.ProfileState.VERIFIED
    assert endpoint.serve_enabled is True
    assert endpoint.capability_revision == 1
    assert marketplace_feed_public_url(endpoint) == before


def test_feed_https_authorities_reject_userinfo_path_and_nonstandard_port(settings):
    for authority in (
        'trusted.example.test@attacker.example',
        'trusted.example.test/path',
        'trusted.example.test?next=attacker.example',
        'trusted.example.test:8443',
    ):
        with pytest.raises(FeedEndpointConfigurationError):
            canonical_marketplace_feed_cdn_origin(authority)

    endpoint = SimpleNamespace(
        public_id='00000000-0000-0000-0000-000000000001',
        account_id=1,
        account=SimpleNamespace(tenant_id=2),
        token_key_id='route-v1',
        previous_token_key_id='',
        owner_identity_digest='a' * 64,
        capability_revision=1,
    )
    settings.MARKETPLACE_FEED_URL_SIGNING_KEYS = {'route-v1': SIGNING_KEY}
    settings.MARKETPLACE_FEED_PUBLIC_BASE_URL = (
        'https://feeds.example.test:443/marketplace-feeds/v1/feed.xml'
    )
    assert urlsplit(marketplace_feed_public_url(endpoint)).netloc == (
        'feeds.example.test'
    )

    settings.MARKETPLACE_FEED_PUBLIC_BASE_URL = (
        'https://feeds.example.test:8443/marketplace-feeds/v1/feed.xml'
    )
    with pytest.raises(FeedEndpointConfigurationError):
        marketplace_feed_public_url(endpoint)


def test_feed_middleware_exempts_only_the_exact_public_capability_path():
    from apps.core.middleware import TenantMiddleware

    middleware = TenantMiddleware(lambda request: None)

    assert middleware._is_public_path('/marketplace-feeds/v1/feed.xml') is True
    assert middleware._is_public_path('/marketplace-feeds/v1/feed.xml/extra') is False
    assert middleware._is_public_path('/marketplace-feeds/other.xml') is False


@pytest.mark.django_db
@override_settings(**ROUTE_SETTINGS)
def test_post_is_deterministic_405_even_with_csrf_enforcement():
    _tenant, account = _account('feed-csrf-exempt')
    endpoint = _endpoint(account)
    parsed = urlsplit(marketplace_feed_public_url(endpoint))

    response = Client(enforce_csrf_checks=True).post(
        f'{parsed.path}?{parsed.query}',
        data=b'ignored',
        content_type='text/plain',
    )

    assert response.status_code == 405
    assert response['Allow'] == 'GET, HEAD'
