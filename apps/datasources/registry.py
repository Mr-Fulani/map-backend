from apps.datasources.adapters.csv_adapter import CSVAdapter
from apps.datasources.adapters.onec_http import OneCHTTPAdapter
from apps.datasources.adapters.onec_xml import OneCXMLAdapter
from apps.datasources.base import BaseDataSourceAdapter

ADAPTER_MAP = {
    '1c_http': OneCHTTPAdapter,
    '1c_xml': OneCXMLAdapter,
    'csv': CSVAdapter,
}


def get_adapter(connection) -> BaseDataSourceAdapter:
    adapter_cls = ADAPTER_MAP.get(connection.type)
    if adapter_cls is None:
        raise ValueError(f'Неизвестный тип источника: {connection.type}')
    return adapter_cls(connection)
