"""Hard wall-clock guards for outbound HTTP work and streamed bodies.

Python cannot safely interrupt a thread that is blocked in the system DNS
resolver (and a few third-party transports have the same limitation).  The
bounded runner therefore returns control at the deadline while retaining a
strict cap on still-running daemon workers.  Once the cap is occupied, new
work waits only for its own remaining budget and then fails closed.
"""

import contextvars
import itertools
import queue
import socket
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import TypeVar, cast


_ResultT = TypeVar('_ResultT')


class HTTPDeadlineExceeded(TimeoutError):
    """Outbound HTTP work exceeded its total wall-clock budget."""


class _BoundedDeadlineRunner:
    """Run blocking operations behind a hard deadline and a fixed admission cap."""

    def __init__(self, *, max_inflight: int):
        if max_inflight < 1:
            raise ValueError('max_inflight must be positive')
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._sequence = itertools.count(1)

    @staticmethod
    def _discard_outcome(
        outcome: tuple[bool, object] | None,
        on_late_result: Callable[[object], None] | None,
    ) -> None:
        if outcome is None or not outcome[0] or on_late_result is None:
            return
        try:
            on_late_result(outcome[1])
        except Exception:
            # Deadline cleanup is best-effort and must never replace the
            # deterministic timeout raised to the original caller.
            pass

    def run(
        self,
        operation: Callable[[], _ResultT],
        *,
        deadline: float,
        on_late_result: Callable[[_ResultT], None] | None = None,
    ) -> _ResultT:
        """Return ``operation`` by ``deadline`` or raise without unbounded workers."""
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._slots.acquire(timeout=remaining):
            raise HTTPDeadlineExceeded('HTTP operation exceeded its wall-clock deadline.')
        # Admission waiting is part of the budget. Do not start a new network
        # operation after a slot becomes available at (or beyond) the deadline.
        if time.monotonic() >= deadline:
            self._slots.release()
            raise HTTPDeadlineExceeded('HTTP operation exceeded its wall-clock deadline.')

        # A one-item handoff plus the state lock closes the race where the
        # worker completes exactly as the caller declares the deadline expired.
        outcomes: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        state_lock = threading.Lock()
        expired = [False]
        caller_context = contextvars.copy_context()

        def worker() -> None:
            try:
                try:
                    if time.monotonic() >= deadline:
                        outcome: tuple[bool, object] = (
                            False,
                            HTTPDeadlineExceeded(
                                'HTTP operation exceeded its wall-clock deadline.',
                            ),
                        )
                    else:
                        outcome = (
                            True,
                            caller_context.run(operation),
                        )
                except BaseException as exc:  # noqa: B036 - re-raised by the caller
                    outcome = (False, exc)

                late_outcome = None
                with state_lock:
                    if expired[0]:
                        late_outcome = outcome
                    else:
                        outcomes.put_nowait(outcome)
                self._discard_outcome(late_outcome, cast(Callable | None, on_late_result))
            finally:
                self._slots.release()

        thread = threading.Thread(
            target=worker,
            name=f'http-deadline-{next(self._sequence)}',
            daemon=True,
        )
        try:
            thread.start()
        except BaseException:
            self._slots.release()
            raise

        def expire_and_take_outcome() -> tuple[bool, object] | None:
            with state_lock:
                expired[0] = True
                try:
                    return outcomes.get_nowait()
                except queue.Empty:
                    return None

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            late_outcome = expire_and_take_outcome()
            self._discard_outcome(late_outcome, cast(Callable | None, on_late_result))
            raise HTTPDeadlineExceeded('HTTP operation exceeded its wall-clock deadline.')

        try:
            outcome = outcomes.get(timeout=remaining)
        except queue.Empty:
            late_outcome = expire_and_take_outcome()
            self._discard_outcome(late_outcome, cast(Callable | None, on_late_result))
            raise HTTPDeadlineExceeded(
                'HTTP operation exceeded its wall-clock deadline.',
            ) from None
        except BaseException:
            late_outcome = expire_and_take_outcome()
            self._discard_outcome(late_outcome, cast(Callable | None, on_late_result))
            raise

        if time.monotonic() > deadline:
            self._discard_outcome(outcome, cast(Callable | None, on_late_result))
            raise HTTPDeadlineExceeded('HTTP operation exceeded its wall-clock deadline.')
        succeeded, value = outcome
        if succeeded:
            return cast(_ResultT, value)
        raise cast(BaseException, value)


# This is deliberately a fixed cap, not a dynamically growing executor.  A
# permanently wedged resolver can consume one slot, but cannot create an
# unbounded number of threads or queued requests.
_DEADLINE_RUNNER = _BoundedDeadlineRunner(max_inflight=32)


def run_with_deadline(
    operation: Callable[[], _ResultT],
    *,
    deadline: float,
    on_late_result: Callable[[_ResultT], None] | None = None,
) -> _ResultT:
    """Run a blocking operation within one absolute monotonic deadline."""
    return _DEADLINE_RUNNER.run(
        operation,
        deadline=deadline,
        on_late_result=on_late_result,
    )


def _nested_attribute(value, path: tuple[str, ...]):
    current = value
    for name in path:
        current = getattr(current, name, None)
        if current is None:
            return None
    return current


def _abort_response(response) -> None:
    """Interrupt a blocked buffered read before falling back to ``close``."""
    raw = getattr(response, 'raw', None)
    candidates = (
        _nested_attribute(raw, ('_fp', 'fp', 'raw', '_sock')),
        _nested_attribute(raw, ('_connection', 'sock')),
        _nested_attribute(raw, ('connection', 'sock')),
    )
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            candidate.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        try:
            candidate.close()
        except (AttributeError, OSError):
            pass
    try:
        response.close()
    except Exception:
        pass


@contextmanager
def enforce_response_deadline(response, seconds: float):
    """Abort an active response when its remaining total budget expires."""
    expired = threading.Event()

    def abort() -> None:
        expired.set()
        _abort_response(response)

    timer = threading.Timer(seconds, abort)
    timer.daemon = True
    timer.start()
    try:
        yield
        if expired.is_set():
            raise HTTPDeadlineExceeded('HTTP response exceeded its wall-clock deadline.')
    except BaseException as exc:
        if expired.is_set() and not isinstance(exc, HTTPDeadlineExceeded):
            raise HTTPDeadlineExceeded(
                'HTTP response exceeded its wall-clock deadline.',
            ) from exc
        raise
    finally:
        timer.cancel()
