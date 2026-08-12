import os
import sys
import time
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HEARTBEAT_FILE = '/tmp/celerybeat-heartbeat'
DJANGO_LIVENESS_HOST = '127.0.0.1'
DJANGO_LIVENESS_PORT = 8000
DJANGO_LIVENESS_PATH = '/api/v1/live/'
DJANGO_LIVENESS_TIMEOUT_SECONDS = 5


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


def django_liveness_is_healthy(connection_factory=HTTPConnection):
    try:
        site_hostname = urlsplit(os.environ.get('SITE_URL', '')).hostname
    except ValueError:
        site_hostname = None
    if not site_hostname:
        return False

    connection = None
    try:
        connection = connection_factory(
            DJANGO_LIVENESS_HOST,
            DJANGO_LIVENESS_PORT,
            timeout=DJANGO_LIVENESS_TIMEOUT_SECONDS,
        )
        connection.request(
            'GET',
            DJANGO_LIVENESS_PATH,
            headers={'Host': site_hostname},
        )
        response = connection.getresponse()
        response.read(1024)
        return response.status == 200
    except (OSError, HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ['celery-beat']:
        return 0 if celery_beat_heartbeat_is_fresh() else 1
    if args == ['django-liveness']:
        return 0 if django_liveness_is_healthy() else 1
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
