import pytest
from django.test import Client
from unittest.mock import patch


@pytest.mark.django_db
def test_health_check():
    """Базовая проверка работоспособности API."""
    client = Client()
    response = client.get('/api/v1/health/')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


@pytest.mark.django_db
def test_liveness_does_not_check_dependencies():
    client = Client()
    with patch('apps.api.urls._database_is_ready') as database_check:
        response = client.get('/api/v1/live/')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}
    database_check.assert_not_called()


@pytest.mark.django_db
@patch('apps.api.urls._cache_is_ready', return_value=True)
@patch('apps.api.urls._database_is_ready', return_value=True)
def test_readiness_requires_database_and_cache(database_check, cache_check):
    response = Client().get('/api/v1/ready/')

    assert response.status_code == 200
    assert response.json() == {'status': 'ready'}
    database_check.assert_called_once_with()
    cache_check.assert_called_once_with()


@pytest.mark.django_db
@patch('apps.api.urls._database_is_ready', side_effect=RuntimeError('db unavailable'))
def test_readiness_returns_503_without_dependency_details(database_check):
    response = Client().get('/api/v1/ready/')

    assert response.status_code == 503
    assert response.json() == {'status': 'unavailable'}
    database_check.assert_called_once_with()
