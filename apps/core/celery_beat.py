import os
from pathlib import Path

from django_celery_beat.schedulers import DatabaseScheduler


DEFAULT_HEARTBEAT_FILE = '/tmp/celerybeat-heartbeat'


class HeartbeatDatabaseScheduler(DatabaseScheduler):
    """Database scheduler that leaves an observable heartbeat after every tick."""

    def tick(self, *args, **kwargs):
        interval = super().tick(*args, **kwargs)
        heartbeat_file = Path(
            os.environ.get('CELERY_BEAT_HEARTBEAT_FILE', DEFAULT_HEARTBEAT_FILE)
        )
        heartbeat_file.touch()
        return interval
