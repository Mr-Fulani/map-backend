"""Security ceilings for synchronous datasource work.

Deployments may lower these values through Django settings.  Raising a value
above the in-code ceiling requires an explicit code review: these operations
run in a web worker and therefore need a predictable CPU and memory budget.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


_SECURITY_CEILINGS = {
    'DATASOURCE_UPLOAD_MAX_BYTES': 5 * 1024 * 1024,
    'DATASOURCE_XLSX_MAX_UNCOMPRESSED_BYTES': 25 * 1024 * 1024,
    'DATASOURCE_XLSX_MAX_ARCHIVE_ENTRIES': 1024,
    'DATASOURCE_IMPORT_MAX_ROWS': 5_000,
    'DATASOURCE_IMPORT_MAX_COLUMNS': 128,
    'DATASOURCE_IMPORT_MAX_CELLS': 100_000,
    'DATASOURCE_XML_MAX_BYTES': 8 * 1024 * 1024,
    'DATASOURCE_HTTP_MAX_BYTES': 5 * 1024 * 1024,
    'DATASOURCE_XML_MAX_NODES': 60_000,
    'DATASOURCE_XML_MAX_TEXT_CHARS': 4 * 1024 * 1024,
    'DATASOURCE_XML_MAX_ITEMS': 5_000,
    'DATASOURCE_FETCH_PAGE_MAX_ITEMS': 500,
}


def datasource_limit(setting_name: str) -> int:
    """Return a positive setting constrained by its reviewed security ceiling."""
    try:
        ceiling = _SECURITY_CEILINGS[setting_name]
    except KeyError as exc:  # pragma: no cover - programming error
        raise ImproperlyConfigured(
            f'Unknown datasource limit setting: {setting_name}',
        ) from exc

    raw_value = getattr(settings, setting_name, ceiling)
    if isinstance(raw_value, bool):
        raise ImproperlyConfigured(f'{setting_name} must be a positive integer.')
    try:
        configured = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(
            f'{setting_name} must be a positive integer.',
        ) from exc
    if configured <= 0:
        raise ImproperlyConfigured(f'{setting_name} must be a positive integer.')
    return min(configured, ceiling)
