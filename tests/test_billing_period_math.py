from datetime import date
from types import SimpleNamespace

import pytest

from apps.billing import services as billing_services


def _subscription(start: date, end: date):
    return SimpleNamespace(
        current_period_start=start,
        current_period_end=end,
    )


@pytest.mark.parametrize(
    ('target', 'expected'),
    [
        (
            date(2026, 2, 27),
            (date(2026, 1, 31), date(2026, 2, 28)),
        ),
        (
            date(2026, 2, 28),
            (date(2026, 2, 28), date(2026, 3, 31)),
        ),
        (
            date(2026, 4, 1),
            (date(2026, 3, 31), date(2026, 4, 15)),
        ),
    ],
)
def test_ai_credit_period_keeps_clamped_month_boundaries(target, expected):
    subscription = _subscription(date(2026, 1, 31), date(2026, 4, 15))

    assert billing_services.ai_credit_period_for_date(
        subscription,
        target,
    ) == expected


def test_ai_credit_period_far_future_uses_constant_number_of_date_shifts(
    monkeypatch,
):
    subscription = _subscription(date(1900, 1, 31), date(9999, 12, 31))
    real_shift = billing_services.add_billing_months
    calls = []

    def counted_shift(anchor, months):
        calls.append((anchor, months))
        return real_shift(anchor, months)

    monkeypatch.setattr(billing_services, 'add_billing_months', counted_shift)

    assert billing_services.ai_credit_period_for_date(
        subscription,
        date(9999, 12, 30),
    ) == (date(9999, 11, 30), date(9999, 12, 31))
    assert len(calls) <= 3


def test_composite_monthly_term_starts_with_one_ai_credit_month():
    subscription = _subscription(date(2026, 1, 15), date(2026, 3, 20))

    assert billing_services.ai_credit_period_for_date(
        subscription,
        date(2026, 1, 15),
    ) == (date(2026, 1, 15), date(2026, 2, 15))
