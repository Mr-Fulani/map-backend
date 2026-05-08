from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from apps.datasources.base import BaseDataSourceAdapter
from apps.datasources.encryption import decrypt


def _session_with_retry() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


class OneCHTTPAdapter(BaseDataSourceAdapter):
    def fetch_changes(self, since: datetime, limit: int = 500, offset: int = 0) -> list[dict]:
        creds = decrypt(self.connection.credentials)
        session = _session_with_retry()
        response = session.get(
            f"{creds['url']}/avito-sync/changes",
            params={'since': since.isoformat(), 'limit': limit, 'offset': offset},
            auth=(creds['user'], creds['password']),
            timeout=30,
        )
        response.raise_for_status()
        return response.json().get('items', [])

    def test_connection(self) -> bool:
        creds = decrypt(self.connection.credentials)
        session = _session_with_retry()
        response = session.get(
            f"{creds['url']}/avito-sync/changes",
            params={'limit': 1},
            auth=(creds['user'], creds['password']),
            timeout=30,
        )
        response.raise_for_status()
        return True

    def get_display_name(self) -> str:
        return '1С HTTP'
