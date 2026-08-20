from io import StringIO

import pytest
from django.core.management import call_command
from django_celery_beat.models import PeriodicTask

from apps.core.queue_observability import (
    COLLECTOR_INTERVAL_SECONDS,
    SNAPSHOT_TTL_SECONDS,
)


@pytest.mark.django_db
def test_periodic_setup_registers_bounded_observability_collector():
    call_command('setup_periodic_tasks', stdout=StringIO())

    task = PeriodicTask.objects.get(name='collect_celery_observability')
    assert task.task == 'apps.core.tasks.collect_celery_observability'
    assert task.queue == 'notifications'
    assert task.interval.every == COLLECTOR_INTERVAL_SECONDS
    assert task.interval.period == 'seconds'
    assert task.expire_seconds == COLLECTOR_INTERVAL_SECONDS - 10
    assert SNAPSHOT_TTL_SECONDS > 2 * COLLECTOR_INTERVAL_SECONDS
