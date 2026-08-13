import pytest
from requests import exceptions as requests_exceptions

from apps.core.provider_boundary import (
    BRAVE_AUTHORITATIVE_REJECTION_STATUSES,
    TAVILY_AUTHORITATIVE_REJECTION_STATUSES,
    is_authoritative_provider_rejection,
    is_proven_pre_send_failure,
)


@pytest.mark.parametrize('status_code', [401, 404, 422])
def test_brave_documented_rejection_is_authoritative(status_code):
    assert is_authoritative_provider_rejection(
        status_code,
        documented_statuses=BRAVE_AUTHORITATIVE_REJECTION_STATUSES,
    ) is True


@pytest.mark.parametrize('status_code', [400, 401])
def test_tavily_documented_rejection_is_authoritative(status_code):
    assert is_authoritative_provider_rejection(
        status_code,
        documented_statuses=TAVILY_AUTHORITATIVE_REJECTION_STATUSES,
    ) is True


@pytest.mark.parametrize(
    'status_code',
    [300, 307, 402, 408, 409, 424, 425, 429, 432, 433, 451, 500, 502, 503],
)
@pytest.mark.parametrize('documented_statuses', [
    BRAVE_AUTHORITATIVE_REJECTION_STATUSES,
    TAVILY_AUTHORITATIVE_REJECTION_STATUSES,
])
def test_undocumented_or_ambiguous_response_is_uncertain(
    status_code,
    documented_statuses,
):
    assert is_authoritative_provider_rejection(
        status_code,
        documented_statuses=documented_statuses,
    ) is False


@pytest.mark.parametrize(
    'exc',
    [
        requests_exceptions.ConnectTimeout(),
        requests_exceptions.InvalidURL(),
        requests_exceptions.InvalidSchema(),
    ],
)
def test_only_proven_pre_send_requests_failures_allow_fallback(exc):
    assert is_proven_pre_send_failure(exc) is True


@pytest.mark.parametrize(
    'exc',
    [
        requests_exceptions.ReadTimeout(),
        requests_exceptions.ConnectionError(),
        requests_exceptions.ChunkedEncodingError(),
        requests_exceptions.InvalidHeader(),
        requests_exceptions.JSONDecodeError('bad json', 'x', 0),
    ],
)
def test_ambiguous_transport_and_response_failures_stop_fallback(exc):
    assert is_proven_pre_send_failure(exc) is False
