from types import SimpleNamespace
from unittest.mock import patch

from apps.marketplaces.views import ListingRefreshBrandCatalogView


def test_brand_catalog_refresh_does_not_expose_provider_exception():
    provider_secret = 'https://client-secret@provider.example/private'
    listing = SimpleNamespace(pk=42, account=object())
    request = SimpleNamespace(tenant=object())

    with (
        patch(
            'apps.marketplaces.views.ListingService.get_for_tenant',
            return_value=listing,
        ),
        patch(
            'apps.marketplaces.adapters.avito.brand_sync.sync_brand_catalog',
            side_effect=RuntimeError(provider_secret),
        ),
        patch('apps.marketplaces.views.logger.warning') as warning,
    ):
        response = ListingRefreshBrandCatalogView().post(request, listing.pk)

    assert response.status_code == 503
    assert response.data == {
        'status': 'error',
        'code': 'catalog_sync_failed',
        'message': 'Не удалось обновить справочник Avito.',
    }
    warning.assert_called_once_with(
        'Avito brand catalog refresh failed for listing=%s (%s).',
        listing.pk,
        'RuntimeError',
    )
    assert provider_secret not in repr(warning.call_args)
