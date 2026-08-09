from types import SimpleNamespace

import pytest
from django.core.cache.backends.locmem import LocMemCache
from rest_framework.exceptions import Throttled

from apps.core import throttling
from apps.core.throttling import (
    CoordinationBackendUnavailable,
    PrincipalScopedRateThrottle,
    TenantScopedRateThrottle,
    consume_tenant_daily_budget,
)


def _request(*, api_key_id=1, tenant_id=1, method='POST'):
    return SimpleNamespace(
        method=method,
        META={},
        user=SimpleNamespace(
            is_authenticated=True,
            is_api_key=True,
            api_key_id=api_key_id,
        ),
        tenant=SimpleNamespace(pk=tenant_id),
    )


def test_principal_throttle_isolated_per_api_key(monkeypatch):
    cache = LocMemCache('principal-throttle-test', {})
    monkeypatch.setattr(PrincipalScopedRateThrottle, 'cache', cache)
    monkeypatch.setattr(
        PrincipalScopedRateThrottle,
        'THROTTLE_RATES',
        {'test-principal': '1/min'},
    )
    view = SimpleNamespace(
        principal_throttle_scope='test-principal',
        expensive_throttle_methods={'POST'},
    )

    assert PrincipalScopedRateThrottle().allow_request(
        _request(api_key_id=10), view,
    ) is True
    assert PrincipalScopedRateThrottle().allow_request(
        _request(api_key_id=10), view,
    ) is False
    assert PrincipalScopedRateThrottle().allow_request(
        _request(api_key_id=11), view,
    ) is True


def test_tenant_throttle_is_shared_by_keys_but_isolated_between_tenants(monkeypatch):
    cache = LocMemCache('tenant-throttle-test', {})
    monkeypatch.setattr(TenantScopedRateThrottle, 'cache', cache)
    monkeypatch.setattr(
        TenantScopedRateThrottle,
        'THROTTLE_RATES',
        {'test-tenant': '1/min'},
    )
    view = SimpleNamespace(
        tenant_throttle_scope='test-tenant',
        expensive_throttle_methods={'POST'},
    )

    assert TenantScopedRateThrottle().allow_request(
        _request(api_key_id=20, tenant_id=7), view,
    ) is True
    assert TenantScopedRateThrottle().allow_request(
        _request(api_key_id=21, tenant_id=7), view,
    ) is False
    assert TenantScopedRateThrottle().allow_request(
        _request(api_key_id=21, tenant_id=8), view,
    ) is True


def test_expensive_throttle_skips_read_methods(monkeypatch):
    class BrokenCache:
        def get(self, *args, **kwargs):
            raise ConnectionError('cache unavailable')

    monkeypatch.setattr(PrincipalScopedRateThrottle, 'cache', BrokenCache())
    view = SimpleNamespace(
        principal_throttle_scope='test-principal',
        expensive_throttle_methods={'POST'},
    )

    assert PrincipalScopedRateThrottle().allow_request(
        _request(method='GET'), view,
    ) is True


def test_expensive_throttle_fails_closed_when_coordination_cache_is_down(monkeypatch):
    class BrokenCache:
        def get(self, *args, **kwargs):
            raise ConnectionError('cache unavailable')

    monkeypatch.setattr(PrincipalScopedRateThrottle, 'cache', BrokenCache())
    monkeypatch.setattr(
        PrincipalScopedRateThrottle,
        'THROTTLE_RATES',
        {'test-principal': '1/min'},
    )
    view = SimpleNamespace(
        principal_throttle_scope='test-principal',
        expensive_throttle_methods={'POST'},
    )

    with pytest.raises(CoordinationBackendUnavailable):
        PrincipalScopedRateThrottle().allow_request(_request(), view)


def test_tenant_daily_budget_enforces_boundary_and_isolates_tenants(monkeypatch):
    cache = LocMemCache('tenant-daily-budget-test', {})
    monkeypatch.setattr(throttling, 'coordination_cache', cache)

    assert consume_tenant_daily_budget(
        tenant_id=31, scope='shared-research', cost=1, limit=2,
    ) == 1
    assert consume_tenant_daily_budget(
        tenant_id=31, scope='shared-research', cost=1, limit=2,
    ) == 2
    with pytest.raises(Throttled):
        consume_tenant_daily_budget(
            tenant_id=31, scope='shared-research', cost=1, limit=2,
        )
    assert consume_tenant_daily_budget(
        tenant_id=32, scope='shared-research', cost=1, limit=2,
    ) == 1


def test_tenant_daily_budget_fails_closed(monkeypatch):
    class BrokenCache:
        def add(self, *args, **kwargs):
            raise ConnectionError('cache unavailable')

    monkeypatch.setattr(throttling, 'coordination_cache', BrokenCache())

    with pytest.raises(CoordinationBackendUnavailable):
        consume_tenant_daily_budget(
            tenant_id=41, scope='image-search', cost=1, limit=100,
        )
