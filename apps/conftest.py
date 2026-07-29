import pytest


@pytest.fixture(autouse=True)
def seed_billing_plans(db):
    """Загружает тарифные планы для каждого теста. Нужны при create_tenant."""
    from django.core.management import call_command
    call_command('seed_plans', verbosity=0)


@pytest.fixture(autouse=True)
def set_encryption_key(settings):
    """Устанавливает тестовый Fernet-ключ для шифрования credentials."""
    settings.FIELD_ENCRYPTION_KEY = 'uhVim_LoYo2vx_SVILD0Hds_4TsWOjZBzaRDh9uTAN0='


@pytest.fixture(autouse=True)
def set_ai_test_provider_key(settings):
    """Маршрутизация в тестах не должна зависеть от локального .env."""
    settings.OPENAI_API_KEY = 'test-openai-key'
