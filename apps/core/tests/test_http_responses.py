import json
import threading
import time

import pytest
import requests

from apps.core.http_deadlines import HTTPDeadlineExceeded, _BoundedDeadlineRunner
from apps.core.http_responses import (
    TrustedResponseDeadlineExceeded,
    TrustedResponseTooLarge,
    UnsupportedResponseEncoding,
    bounded_http_request,
)


class ChunkedResponse(requests.Response):
    def __init__(self, chunks, *, headers=None):
        super().__init__()
        self.status_code = 200
        self.headers.update(headers or {})
        self._chunks = chunks
        self.was_closed = False
        self.closed_event = threading.Event()

    def iter_content(self, chunk_size=1, decode_unicode=False):
        del chunk_size, decode_unicode
        yield from self._chunks

    def close(self):
        self.was_closed = True
        self.closed_event.set()


class BlockingResponse(ChunkedResponse):
    def __init__(self):
        super().__init__([])
        self.release = threading.Event()

    def iter_content(self, chunk_size=1, decode_unicode=False):
        del chunk_size, decode_unicode
        assert self.release.wait(timeout=1)
        yield b'late'

    def close(self):
        super().close()
        self.release.set()


def test_bounded_http_request_buffers_only_allowed_bytes_and_closes():
    response = ChunkedResponse([b'{"ok":', b'true}'])
    captured = {}

    def requester(url, **kwargs):
        captured.update(kwargs)
        return response

    result = bounded_http_request(requester, 'https://api.example.test', max_bytes=32)

    assert result.json() == {'ok': True}
    assert response.closed_event.wait(timeout=0.5)
    assert captured['stream'] is True
    assert captured['allow_redirects'] is False
    assert captured['headers']['Accept-Encoding'] == 'identity'


def test_bounded_http_request_rejects_streamed_overflow_and_closes():
    response = ChunkedResponse([b'1234', b'56'])

    with pytest.raises(TrustedResponseTooLarge):
        bounded_http_request(lambda *args, **kwargs: response, 'https://api.test', max_bytes=5)

    assert response.was_closed is True


def test_bounded_http_request_rejects_compressed_body_before_reading():
    response = ChunkedResponse(
        [json.dumps({'large': 'payload'}).encode()],
        headers={'Content-Encoding': 'gzip'},
    )

    with pytest.raises(UnsupportedResponseEncoding):
        bounded_http_request(lambda *args, **kwargs: response, 'https://api.test', max_bytes=1024)

    assert response.was_closed is True


def test_bounded_http_request_rejects_oversized_content_length():
    response = ChunkedResponse([], headers={'Content-Length': '100'})

    with pytest.raises(TrustedResponseTooLarge):
        bounded_http_request(lambda *args, **kwargs: response, 'https://api.test', max_bytes=99)

    assert response.was_closed is True


@pytest.mark.parametrize('value', ['not-a-number', '-1'])
def test_bounded_http_request_rejects_invalid_content_length(value):
    response = ChunkedResponse([], headers={'Content-Length': value})

    with pytest.raises(ValueError):
        bounded_http_request(
            lambda *args, **kwargs: response,
            'https://api.test',
            max_bytes=100,
        )

    assert response.was_closed is True


def test_bounded_http_request_aborts_slow_drip_body_at_total_deadline():
    response = BlockingResponse()

    with pytest.raises(TrustedResponseDeadlineExceeded):
        bounded_http_request(
            lambda *args, **kwargs: response,
            'https://api.test',
            max_bytes=100,
            max_elapsed_seconds=0.02,
        )

    assert response.closed_event.wait(timeout=0.5)


def test_bounded_http_request_deadline_includes_waiting_for_response_headers():
    response = ChunkedResponse([b'late'])
    requester_entered = threading.Event()
    release_requester = threading.Event()

    def slow_requester(*_args, **_kwargs):
        requester_entered.set()
        release_requester.wait(timeout=1)
        return response

    started_at = time.monotonic()
    try:
        with pytest.raises(TrustedResponseDeadlineExceeded):
            bounded_http_request(
                slow_requester,
                'https://api.test',
                max_bytes=100,
                max_elapsed_seconds=0.02,
            )
    finally:
        release_requester.set()

    assert requester_entered.is_set()
    assert time.monotonic() - started_at < 0.5
    assert response.closed_event.wait(timeout=0.5)


def test_deadline_runner_fails_closed_when_its_worker_cap_is_occupied():
    runner = _BoundedDeadlineRunner(max_inflight=1)
    release_first = threading.Event()
    first_cleaned = threading.Event()
    second_started = threading.Event()
    marker = object()

    def blocked_operation():
        release_first.wait(timeout=1)
        return marker

    with pytest.raises(HTTPDeadlineExceeded):
        runner.run(
            blocked_operation,
            deadline=time.monotonic() + 0.02,
            on_late_result=lambda result: (
                first_cleaned.set() if result is marker else None
            ),
        )

    try:
        with pytest.raises(HTTPDeadlineExceeded):
            runner.run(
                lambda: second_started.set(),
                deadline=time.monotonic() + 0.02,
            )
    finally:
        release_first.set()

    assert second_started.is_set() is False
    assert first_cleaned.wait(timeout=0.5)


def test_deadline_runner_does_not_start_operation_after_expired_admission():
    runner = _BoundedDeadlineRunner(max_inflight=1)
    operation_started = threading.Event()

    with pytest.raises(HTTPDeadlineExceeded):
        runner.run(
            lambda: operation_started.set(),
            deadline=time.monotonic() - 0.001,
        )

    assert operation_started.is_set() is False
