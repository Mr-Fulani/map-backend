from unittest.mock import MagicMock, patch

import pytest

from apps.marketplaces.adapters.avito.adapter import AvitoAdapter
from apps.marketplaces.adapters.avito.rate_limiter import (
    AUTOLOAD_RATE_LIMIT_RETRY_AFTER,
    RateLimitError,
)


def test_autoload_429_raises_rate_limit_with_numeric_retry_delay():
    account = MagicMock()
    response = MagicMock(status_code=429, text='one upload per hour')

    with patch(
        'apps.marketplaces.adapters.avito.adapter._avito_request',
        return_value=response,
    ):
        adapter = AvitoAdapter(account)
        adapter._auth.get_token = MagicMock(return_value='token')
        adapter._stable_feed_locator = MagicMock(return_value=None)

        with pytest.raises(RateLimitError) as error:
            adapter._trigger_autoload()

    adapter._stable_feed_locator.assert_called_once_with(
        require_serve_enabled=True,
    )
    assert error.value.retry_after == AUTOLOAD_RATE_LIMIT_RETRY_AFTER
    assert isinstance(error.value.retry_after, int)
