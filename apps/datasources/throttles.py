"""Fail-closed per-principal and per-tenant datasource throttles."""

from django.conf import settings

from apps.core.throttling import (
    PrincipalScopedRateThrottle,
    TenantScopedRateThrottle,
)


DEFAULT_DATASOURCE_THROTTLE_RATES = {
    'datasource_test_principal': '5/min',
    'datasource_test_tenant': '10/min',
    'datasource_sync_principal': '2/min',
    'datasource_sync_tenant': '4/min',
    'datasource_upload_principal': '2/hour',
    'datasource_upload_tenant': '6/hour',
}


class _DataSourceRateMixin:
    def get_rate(self):
        configured = getattr(settings, 'REST_FRAMEWORK', {}).get(
            'DEFAULT_THROTTLE_RATES', {},
        ).get(self.scope)
        return configured or DEFAULT_DATASOURCE_THROTTLE_RATES.get(self.scope)


class DataSourcePrincipalRateThrottle(
    _DataSourceRateMixin,
    PrincipalScopedRateThrottle,
):
    pass


class DataSourceTenantRateThrottle(
    _DataSourceRateMixin,
    TenantScopedRateThrottle,
):
    pass
