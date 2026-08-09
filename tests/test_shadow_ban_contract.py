from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from apps.anti_ban.shadow_ban import ShadowBanDetector


def _account_with_item_ids(item_ids: list[str]) -> MagicMock:
    account = MagicMock()
    account.name = 'Test account'
    account.pk = 42
    queryset = account.listings.filter.return_value.exclude.return_value
    queryset.values_list.return_value = item_ids
    return account


def test_detector_uses_current_avito_stats_contract_and_aggregates_seven_days():
    account = _account_with_item_ids(['101', '202'])
    detector = ShadowBanDetector()
    raw_stats = [
        {
            'itemId': 101,
            'stats': [
                {'date': '2026-08-03', 'views': 400, 'uniqViews': 1},
                {'date': '2026-08-04', 'views': 100, 'uniqViews': 0},
            ],
        },
        {
            'itemId': 202,
            'stats': [
                {'date': '2026-08-05', 'views': 100, 'uniqViews': 1},
            ],
        },
    ]

    with patch(
        'apps.anti_ban.shadow_ban.localdate',
        return_value=date(2026, 8, 9),
    ), patch(
        'apps.marketplaces.adapters.avito.adapter.AvitoAdapter',
    ) as adapter_class, patch.object(detector, '_log_warning') as log_warning:
        adapter_class.return_value.get_stats.return_value = raw_stats

        result = detector.check_account(account)

    adapter_class.assert_called_once_with(account)
    adapter_class.return_value.get_stats.assert_called_once_with(
        ['101', '202'],
        date(2026, 8, 3),
        date(2026, 8, 9),
    )
    assert result == {
        'shadow_ban_suspected': True,
        'ctr': 0.0033,
        'views': 600,
    }
    log_warning.assert_called_once_with(account, 2 / 600, 600)


def test_detector_skips_provider_when_account_has_no_active_external_items():
    account = _account_with_item_ids([])

    with patch(
        'apps.marketplaces.adapters.avito.adapter.AvitoAdapter',
    ) as adapter_class:
        result = ShadowBanDetector().check_account(account)

    adapter_class.assert_not_called()
    assert result == {
        'shadow_ban_suspected': False,
        'ctr': 0.0,
        'views': 0,
    }


def test_detector_propagates_provider_failure_for_task_retry():
    account = _account_with_item_ids(['101'])

    with patch(
        'apps.marketplaces.adapters.avito.adapter.AvitoAdapter',
    ) as adapter_class:
        adapter_class.return_value.get_stats.side_effect = RuntimeError('provider unavailable')

        with pytest.raises(RuntimeError, match='provider unavailable'):
            ShadowBanDetector().check_account(account)


@pytest.mark.parametrize('value', [True, -1, 1.5, '10'])
def test_detector_rejects_malformed_counters(value):
    with pytest.raises(ValueError, match='счётчик views'):
        ShadowBanDetector._required_count(value, 'views')
