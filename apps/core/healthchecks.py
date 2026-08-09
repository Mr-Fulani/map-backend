import os
import sys
import time
from pathlib import Path

DEFAULT_HEARTBEAT_FILE = '/tmp/celerybeat-heartbeat'
def celery_beat_heartbeat_is_fresh(now=None):
    heartbeat_file = Path(
        os.environ.get('CELERY_BEAT_HEARTBEAT_FILE', DEFAULT_HEARTBEAT_FILE)
    )
    max_age = int(os.environ.get('CELERY_BEAT_HEARTBEAT_MAX_AGE_SECONDS', '120'))
    if max_age <= 0:
        return False

    try:
        modified_at = heartbeat_file.stat().st_mtime
    except OSError:
        return False

    age = (time.time() if now is None else now) - modified_at
    return 0 <= age <= max_age


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ['celery-beat']:
        return 0 if celery_beat_heartbeat_is_fresh() else 1
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
