import pytest


@pytest.fixture(autouse=True)
def enable_billing_for_billing_feature_tests(settings):
    """Billing feature tests opt in; the application-wide default is fail-closed."""
    settings.BILLING_ENABLED = True
