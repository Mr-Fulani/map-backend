import pytest
from django.test import Client


@pytest.mark.django_db
def test_health_check():
    """Базовая проверка работоспособности API."""
    client = Client()
    response = client.get('/api/v1/health/')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'
