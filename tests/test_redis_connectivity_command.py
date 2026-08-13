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
def test_command_probes_capacity_and_writes_every_runtime_redis_url(client_factory):
    clients = [MagicMock() for _ in REDIS_SETTINGS]
    for setting_name, client in zip(REDIS_SETTINGS, clients, strict=True):
        client.ping.return_value = True
        client.info.return_value = {
            'used_memory': 1024,
            'maxmemory': check_redis_connectivity.EXPECTED_MAXMEMORY_BYTES[setting_name],
        }
        client.set.return_value = True
        client.delete.return_value = 1
    client_factory.side_effect = clients
    output = StringIO()

    check_redis_connectivity.Command(stdout=output).handle()

    assert [call.args[0] for call in client_factory.call_args_list] == list(
        REDIS_SETTINGS.values()
    )
    for client in clients:
        client.ping.assert_called_once_with()
        client.info.assert_called_once_with(section='memory')
        client.set.assert_called_once()
        assert client.set.call_args.kwargs == {'ex': 10, 'nx': True}
        assert client.delete.call_count >= 1
        client.close.assert_called_once_with()
    assert output.getvalue().count(': ok') == len(REDIS_SETTINGS)
    assert check_redis_connectivity.Command.requires_system_checks == []


@patch.object(check_redis_connectivity, 'settings', SimpleNamespace(**REDIS_SETTINGS))
@patch.object(check_redis_connectivity, '_redis_client')
def test_command_fails_closed_without_leaking_redis_credentials(client_factory):
    cache_client = MagicMock()
    cache_client.ping.return_value = True
    cache_client.info.return_value = {
        'used_memory': 1024,
        'maxmemory': 160 * 1024 * 1024,
    }
    cache_client.set.return_value = True
    cache_client.delete.return_value = 1
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


@patch.object(check_redis_connectivity, 'settings', SimpleNamespace(**REDIS_SETTINGS))
@patch.object(check_redis_connectivity, '_redis_client')
def test_command_rejects_projected_oom_and_target_limit_drift(client_factory):
    projected_oom = MagicMock()
    projected_oom.ping.return_value = True
    projected_oom.info.return_value = {
        'used_memory': 150 * 1024 * 1024,
        'maxmemory': 1024 * 1024 * 1024,
    }
    client_factory.return_value = projected_oom

    with pytest.raises(CommandError):
        check_redis_connectivity.Command().handle()
    projected_oom.set.assert_not_called()

    wrong_target = MagicMock()
    wrong_target.ping.return_value = True
    wrong_target.info.return_value = {
        'used_memory': 1024,
        'maxmemory': 512 * 1024 * 1024,
    }
    wrong_target.set.return_value = True
    wrong_target.delete.return_value = 1
    client_factory.return_value = wrong_target

    with pytest.raises(CommandError):
        check_redis_connectivity.Command().handle(require_target_limits=True)
    wrong_target.set.assert_not_called()
