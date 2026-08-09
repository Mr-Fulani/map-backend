from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.core.management.base import CommandError

from apps.core.management.commands import check_redis_connectivity


REDIS_SETTINGS = {
    'CACHE_REDIS_URL': 'redis://:cache-secret@redis:6379/0',
    'CELERY_BROKER_URL': 'redis://:broker-secret@redis_broker:6379/0',
    'CELERY_RESULT_BACKEND': 'redis://:broker-secret@redis_broker:6379/1',
    'COORDINATION_REDIS_URL': 'redis://:broker-secret@redis_broker:6379/2',
}


@patch.object(check_redis_connectivity, 'settings', SimpleNamespace(**REDIS_SETTINGS))
@patch.object(check_redis_connectivity, '_redis_client')
def test_command_pings_every_runtime_redis_url_without_writes(client_factory):
    clients = [MagicMock() for _ in REDIS_SETTINGS]
    for client in clients:
        client.ping.return_value = True
    client_factory.side_effect = clients
    output = StringIO()

    check_redis_connectivity.Command(stdout=output).handle()

    assert [call.args[0] for call in client_factory.call_args_list] == list(
        REDIS_SETTINGS.values()
    )
    for client in clients:
        client.ping.assert_called_once_with()
        client.close.assert_called_once_with()
    assert output.getvalue().count(': ok') == len(REDIS_SETTINGS)
    assert check_redis_connectivity.Command.requires_system_checks == []


@patch.object(check_redis_connectivity, 'settings', SimpleNamespace(**REDIS_SETTINGS))
@patch.object(check_redis_connectivity, '_redis_client')
def test_command_fails_closed_without_leaking_redis_credentials(client_factory):
    cache_client = MagicMock()
    cache_client.ping.return_value = True
    broker_client = MagicMock()
    broker_client.ping.side_effect = ConnectionError(
        'redis://:broker-secret@redis_broker:6379/0 is unavailable'
    )
    client_factory.side_effect = [cache_client, broker_client]

    with pytest.raises(CommandError) as error:
        check_redis_connectivity.Command().handle()

    assert 'CELERY_BROKER_URL' in str(error.value)
    assert 'broker-secret' not in str(error.value)
    cache_client.close.assert_called_once_with()
    broker_client.close.assert_called_once_with()
