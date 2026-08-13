from collections.abc import Callable

from apps.datasources.adapters.csv_adapter import CSVAdapter
from apps.datasources.adapters.onec_http import OneCHTTPAdapter
from apps.datasources.adapters.onec_xml import OneCXMLAdapter
from apps.datasources.base import BaseDataSourceAdapter

ADAPTER_MAP: dict[str, Callable[[object], BaseDataSourceAdapter]] = {
    '1c_http': OneCHTTPAdapter,
    '1c_xml': OneCXMLAdapter,
    'csv': CSVAdapter,
}


def get_adapter(connection) -> BaseDataSourceAdapter:
    adapter_factory = ADAPTER_MAP.get(connection.type)
    if adapter_factory is None:
        raise ValueError(f'Неизвестный тип источника: {connection.type}')
    return adapter_factory(connection)
