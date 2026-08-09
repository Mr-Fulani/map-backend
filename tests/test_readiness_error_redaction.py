from unittest.mock import patch

from django.test import RequestFactory

from apps.api.urls import readiness_check


def test_readiness_failure_does_not_log_dependency_credentials():
    secret = 'postgresql://user:database-secret@db:5432/app'

    with (
        patch(
            'apps.api.urls._database_is_ready',
            side_effect=RuntimeError(secret),
        ),
        patch('apps.api.urls.logger.warning') as warning,
    ):
        response = readiness_check(RequestFactory().get('/api/v1/ready/'))

    assert response.status_code == 503
    warning.assert_called_once_with(
        'Readiness dependency check failed (%s).',
        'RuntimeError',
    )
    assert secret not in repr(warning.call_args)
