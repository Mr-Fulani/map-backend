from io import StringIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.management.base import CommandError

from apps.core.management.commands import check_email_connectivity


@patch.object(check_email_connectivity, 'get_connection')
def test_command_opens_authenticated_connection_without_sending_email(connection_factory):
    connection = MagicMock()
    connection.open.return_value = True
    connection_factory.return_value = connection
    output = StringIO()

    check_email_connectivity.Command(stdout=output).handle()

    connection_factory.assert_called_once_with(fail_silently=False)
    connection.open.assert_called_once_with()
    connection.close.assert_called_once_with()
    assert 'SMTP connectivity and credentials: ok' in output.getvalue()
    assert check_email_connectivity.Command.requires_system_checks == []


@patch.object(check_email_connectivity, 'get_connection')
def test_command_fails_closed_without_leaking_provider_or_credentials(
    connection_factory,
):
    connection = MagicMock()
    connection.open.side_effect = OSError(
        'smtp.sendpulse.com login user@example.test password=provider-secret'
    )
    connection_factory.return_value = connection

    with pytest.raises(CommandError) as error:
        check_email_connectivity.Command().handle()

    assert 'OSError' in str(error.value)
    assert 'smtp.sendpulse.com' not in str(error.value)
    assert 'user@example.test' not in str(error.value)
    assert 'provider-secret' not in str(error.value)
    connection.close.assert_called_once_with()
