import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.db import close_old_connections, connection

from apps.core.advisory_lock import try_session_advisory_lock
from apps.core.dispatch import SafeRetryableDispatchError


def test_session_advisory_lock_releases_on_exception():
    cursor = MagicMock()
    cursor.fetchone.side_effect = [(True,), (True,)]
    cursor_context = MagicMock()
    cursor_context.__enter__.return_value = cursor
    fake_connection = MagicMock(vendor='postgresql')
    fake_connection.cursor.return_value = cursor_context

    with patch('apps.core.advisory_lock.connection', fake_connection):
        with pytest.raises(RuntimeError, match='local apply failed'):
            with try_session_advisory_lock('workflow:exception') as acquired:
                assert acquired is True
                raise RuntimeError('local apply failed')

    sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert sql == [
        'SELECT pg_try_advisory_lock(%s)',
        'SELECT pg_advisory_unlock(%s)',
    ]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize('consumer', ['image', 'euroauto'])
def test_provider_checkpoint_consumers_serialize_concurrent_replay_workers(
    consumer,
):
    """A second replay owner exits before provider/local apply entry."""
    if connection.vendor != 'postgresql':
        pytest.skip('session advisory-lock concurrency requires PostgreSQL')

    entered = threading.Event()
    release_owner = threading.Event()
    owner_finished = threading.Event()
    calls = []
    first_error: list[BaseException] = []

    def owned(*args, **kwargs):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            entered.set()
            assert release_owner.wait(timeout=10)
        return {'ok': True}

    if consumer == 'image':
        from apps.image_search.services import pipeline

        invoke = lambda: pipeline.run_for_product(  # noqa: E731
            SimpleNamespace(),
            workflow_key='image-search-task:991',
            tracking_id=991,
        )
        target = patch.object(pipeline, '_run_for_product_owned', side_effect=owned)
    else:
        from apps.products.services import ProductEnrichmentService

        invoke = lambda: ProductEnrichmentService.run_parse_job(992)  # noqa: E731
        target = patch.object(
            ProductEnrichmentService,
            '_run_parse_job_owned',
            side_effect=owned,
        )

    def first_worker():
        close_old_connections()
        try:
            invoke()
        except Exception as exc:  # pragma: no cover - asserted below
            first_error.append(exc)
        finally:
            owner_finished.set()
            close_old_connections()

    with target:
        worker = threading.Thread(target=first_worker, daemon=True)
        worker.start()
        assert entered.wait(timeout=10)

        with pytest.raises(
            SafeRetryableDispatchError,
            match='already owned by another worker',
        ):
            invoke()
        assert len(calls) == 1

        release_owner.set()
        assert owner_finished.wait(timeout=10)
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert first_error == []

        # ``finally`` released the session lock, so a later durable replay can
        # enter local apply (and restore a checkpoint) without provider overlap.
        assert invoke() == {'ok': True}
        assert len(calls) == 2
