from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.core.management.base import CommandError

from apps.core.management.commands import check_public_http_connectivity
from apps.core.url_security import REDIRECT_NONE


YOOKASSA_SETTINGS = SimpleNamespace(
    BILLING_ENABLED=True,
    YOOKASSA_SHOP_ID='shop-id',
    YOOKASSA_SECRET_KEY='provider-secret',
    YOOKASSA_API_CONNECT_TIMEOUT_SECONDS=3.05,
    YOOKASSA_API_READ_TIMEOUT_SECONDS=10,
    YOOKASSA_API_MAX_ELAPSED_SECONDS=30,
)

DISABLED_BILLING_SETTINGS = SimpleNamespace(
    BILLING_ENABLED=False,
    YOOKASSA_API_CONNECT_TIMEOUT_SECONDS=3.05,
    YOOKASSA_API_READ_TIMEOUT_SECONDS=10,
    YOOKASSA_API_MAX_ELAPSED_SECONDS=30,
)


@patch.object(check_public_http_connectivity, 'settings', YOOKASSA_SETTINGS)
@patch.object(check_public_http_connectivity, 'request_public_http_url')
def test_command_uses_side_effect_free_bounded_yookassa_get(public_request):
    public_request.return_value = MagicMock(status_code=404)
    output = StringIO()

    check_public_http_connectivity.Command(stdout=output).handle()

    public_request.assert_called_once_with(
        check_public_http_connectivity.YOOKASSA_PREFLIGHT_URL,
        method='GET',
        timeout=(3.05, 10),
        auth=('shop-id', 'provider-secret'),
        status_only=True,
        redirect_policy=REDIRECT_NONE,
        max_redirects=0,
        max_elapsed_seconds=30,
    )
    assert 'Public HTTPS transport and YooKassa credentials: ok' in output.getvalue()
    assert check_public_http_connectivity.Command.requires_system_checks == []


@patch.object(check_public_http_connectivity, 'settings', YOOKASSA_SETTINGS)
@patch.object(check_public_http_connectivity, 'request_public_http_url')
def test_command_fails_closed_without_leaking_provider_details(public_request):
    public_request.side_effect = OSError(
        'https://api.yookassa.ru/v3 provider-secret is unavailable'
    )

    with pytest.raises(CommandError) as error:
        check_public_http_connectivity.Command().handle()

    assert 'OSError' in str(error.value)
    assert 'api.yookassa.ru' not in str(error.value)
    assert 'provider-secret' not in str(error.value)


@patch.object(check_public_http_connectivity, 'settings', YOOKASSA_SETTINGS)
@patch.object(check_public_http_connectivity, 'request_public_http_url')
def test_command_requires_authenticated_not_found_sentinel(public_request):
    public_request.return_value = MagicMock(status_code=401)

    with pytest.raises(CommandError) as error:
        check_public_http_connectivity.Command().handle()

    assert 'RuntimeError' in str(error.value)
    assert '401' not in str(error.value)


@patch.object(
    check_public_http_connectivity,
    'settings',
    DISABLED_BILLING_SETTINGS,
)
@patch.object(check_public_http_connectivity, 'request_public_http_url')
def test_command_still_checks_public_transport_when_billing_is_disabled(
    public_request,
):
    public_request.return_value = MagicMock(status_code=401)
    output = StringIO()

    check_public_http_connectivity.Command(stdout=output).handle()

    assert public_request.call_args.kwargs['auth'] is None
    assert 'Public HTTPS transport: ok (billing disabled)' in output.getvalue()
