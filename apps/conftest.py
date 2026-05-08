import pytest


@pytest.fixture(autouse=True)
def seed_billing_plans(db):
    """Загружает тарифные планы для каждого теста. Нужны при create_tenant."""
    from django.core.management import call_command
    call_command('seed_plans', verbosity=0)
