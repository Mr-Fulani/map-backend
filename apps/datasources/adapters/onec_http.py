from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from apps.core.url_security import (
    REDIRECT_SAME_ORIGIN,
    UnsafePublicURL,
    request_public_http_url,
)
from apps.datasources.base import BaseDataSourceAdapter
from apps.datasources.encryption import decrypt
from apps.datasources.limits import datasource_limit
from apps.datasources.validation import validate_onec_credentials, validate_onec_https_url


class OneCHTTPValidationError(ValueError):
    pass


def _changes_url(base_url: str) -> str:
    base_url = validate_onec_https_url(base_url)
    try:
        parsed = urlsplit(base_url)
    except ValueError as exc:
        raise UnsafePublicURL('Некорректный URL источника 1С.') from exc
    if parsed.query or parsed.fragment:
        raise UnsafePublicURL('Base URL источника 1С не должен содержать query или fragment.')
    path = f'{parsed.path.rstrip("/")}/avito-sync/changes'
    return urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))


def _validate_pagination(limit: int, offset: int) -> None:
    max_page_items = datasource_limit('DATASOURCE_FETCH_PAGE_MAX_ITEMS')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= max_page_items:
        raise OneCHTTPValidationError(
            f'limit должен быть целым числом от 1 до {max_page_items}.',
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise OneCHTTPValidationError('offset должен быть неотрицательным целым числом.')


class OneCHTTPAdapter(BaseDataSourceAdapter):
    def _request(self, *, params: dict, status_only: bool = False):
        creds = validate_onec_credentials(decrypt(self.connection.credentials))
        return request_public_http_url(
            _changes_url(creds['url']),
            timeout=(5, 30),
            params=params,
            auth=(creds.get('user', ''), creds.get('password', '')),
            max_response_bytes=(
                None
                if status_only
                else datasource_limit('DATASOURCE_HTTP_MAX_BYTES')
            ),
            status_only=status_only,
            redirect_policy=REDIRECT_SAME_ORIGIN,
        )

    def fetch_changes(
        self,
        since: datetime,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict]:
        _validate_pagination(limit, offset)
        response = self._request(params={
            'since': since.isoformat(),
            'limit': limit,
            'offset': offset,
        })
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise OneCHTTPValidationError('Источник 1С вернул некорректный JSON.') from exc
        if not isinstance(payload, dict) or not isinstance(payload.get('items', []), list):
            raise OneCHTTPValidationError('Источник 1С вернул некорректную структуру JSON.')
        items = payload.get('items', [])
        if len(items) > max(0, limit):
            raise OneCHTTPValidationError('Источник 1С превысил запрошенный лимит строк.')
        if any(not isinstance(item, dict) for item in items):
            raise OneCHTTPValidationError('Элементы ответа источника 1С должны быть объектами.')
        return items

    def test_connection(self) -> bool:
        response = self._request(params={'limit': 1}, status_only=True)
        response.raise_for_status()
        return True

    def get_display_name(self) -> str:
        return '1С HTTP'
