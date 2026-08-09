import os
import tempfile
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import openpyxl
import pytest
from django.core.cache.backends.locmem import LocMemCache

from apps.datasources.adapters.csv_adapter import CSVAdapter, CSVValidationError
from apps.datasources.adapters.onec_http import (
    OneCHTTPAdapter,
    OneCHTTPValidationError,
)
from apps.datasources.adapters.onec_xml import (
    OneCXMLAdapter,
    OneCXMLValidationError,
    _parse_items,
)
from apps.datasources.encryption import encrypt
from apps.datasources.limits import datasource_limit
from apps.datasources.serializers import (
    DataSourceConnectionSerializer,
    DataSourceConnectionUpdateSerializer,
)
from apps.datasources.throttles import (
    DataSourcePrincipalRateThrottle,
    DataSourceTenantRateThrottle,
)
from apps.datasources.views import (
    CSVUploadView,
    DataSourceSyncView,
    DataSourceTestView,
    _save_uploaded_file,
    _validated_upload_name,
)


class MisreportedUpload:
    size = 1

    @staticmethod
    def chunks():
        yield b'a' * 6
        yield b'b' * 5


def _write_csv(content: str) -> str:
    source = tempfile.NamedTemporaryFile(
        mode='w', suffix='.csv', delete=False, encoding='utf-8',
    )
    source.write(content)
    source.close()
    return source.name


def _write_xlsx(rows: list[list]) -> str:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    for row in rows:
        worksheet.append(row)
    source = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
    source.close()
    workbook.save(source.name)
    workbook.close()
    return source.name


def _xml_connection(url: str):
    connection = MagicMock()
    connection.credentials = encrypt({
        'url': url,
        'user': 'xml-user',
        'password': 'xml-password',
    })
    return connection


def _xml_response(payload: bytes):
    response = MagicMock(status_code=200, headers={})
    response.content = payload
    return response


def _request(*, user_id=1, tenant_id=1):
    return SimpleNamespace(
        method='POST',
        META={},
        user=SimpleNamespace(
            is_authenticated=True,
            is_api_key=False,
            pk=user_id,
        ),
        tenant=SimpleNamespace(pk=tenant_id),
    )


def test_upload_uses_actual_chunk_size_and_removes_partial_file(settings, tmp_path, monkeypatch):
    settings.DATASOURCE_UPLOAD_MAX_BYTES = 10
    real_named_temporary_file = tempfile.NamedTemporaryFile

    def named_temporary_file(**kwargs):
        return real_named_temporary_file(dir=tmp_path, **kwargs)

    monkeypatch.setattr(
        'apps.datasources.views.tempfile.NamedTemporaryFile',
        named_temporary_file,
    )

    with pytest.raises(CSVValidationError, match='Размер файла превышает'):
        _save_uploaded_file(MisreportedUpload(), '.csv')

    assert list(tmp_path.iterdir()) == []


def test_upload_name_rejects_non_catalog_formats():
    upload = SimpleNamespace(name='payload.pdf')
    with pytest.raises(CSVValidationError, match='только файлы'):
        _validated_upload_name(upload)


def test_synchronous_import_settings_cannot_exceed_reviewed_ceilings(settings):
    settings.DATASOURCE_UPLOAD_MAX_BYTES = 100 * 1024 * 1024
    settings.DATASOURCE_IMPORT_MAX_ROWS = 1_000_000

    assert datasource_limit('DATASOURCE_UPLOAD_MAX_BYTES') == 5 * 1024 * 1024
    assert datasource_limit('DATASOURCE_IMPORT_MAX_ROWS') == 5_000


def test_adapter_rejects_oversized_file_when_called_without_view(settings):
    settings.DATASOURCE_UPLOAD_MAX_BYTES = 10
    path = _write_csv('article,name,price,stock_qty\n')
    try:
        with pytest.raises(CSVValidationError, match='Размер файла превышает'):
            CSVAdapter(connection=None).process_uploaded_file(path)
    finally:
        os.unlink(path)


def test_csv_physical_row_limit_is_enforced(settings):
    settings.DATASOURCE_IMPORT_MAX_ROWS = 2
    path = _write_csv(
        'article,name,price,stock_qty\n'
        'A1,One,1,1\n'
        'A2,Two,2,2\n'
    )
    try:
        with pytest.raises(CSVValidationError, match='Количество строк'):
            CSVAdapter(connection=None).process_uploaded_file(path)
    finally:
        os.unlink(path)


def test_csv_column_limit_is_enforced(settings):
    settings.DATASOURCE_IMPORT_MAX_COLUMNS = 4
    path = _write_csv('article,name,price,stock_qty,unexpected\nA1,One,1,1,x\n')
    try:
        with pytest.raises(CSVValidationError, match='Количество колонок'):
            CSVAdapter(connection=None).process_uploaded_file(path)
    finally:
        os.unlink(path)


def test_xlsx_total_cell_limit_is_enforced(settings):
    settings.DATASOURCE_IMPORT_MAX_CELLS = 7
    path = _write_xlsx([
        ['article', 'name', 'price', 'stock_qty'],
        ['A1', 'One', 1, 1],
    ])
    try:
        with pytest.raises(CSVValidationError, match='Количество ячеек'):
            CSVAdapter(connection=None).process_uploaded_file(path)
    finally:
        os.unlink(path)


def test_xlsx_expanded_archive_limit_is_checked_before_openpyxl(settings):
    settings.DATASOURCE_XLSX_MAX_UNCOMPRESSED_BYTES = 10
    path = _write_xlsx([
        ['article', 'name', 'price', 'stock_qty'],
        ['A1', 'One', 1, 1],
    ])
    try:
        with patch('apps.datasources.adapters.csv_adapter.openpyxl.load_workbook') as load:
            with pytest.raises(CSVValidationError, match='Распакованный размер'):
                CSVAdapter(connection=None).process_uploaded_file(path)
        load.assert_not_called()
    finally:
        os.unlink(path)


def test_sparse_sheet_declared_dimensions_are_rejected_before_iteration(settings):
    settings.DATASOURCE_IMPORT_MAX_ROWS = 100
    settings.DATASOURCE_IMPORT_MAX_CELLS = 1000

    with pytest.raises(CSVValidationError, match='Количество ячеек'):
        CSVAdapter._validate_sheet_dimensions(10, 128)


def test_onec_xml_rejects_private_source_before_request():
    adapter = OneCXMLAdapter(_xml_connection('https://127.0.0.1/feed.xml'))

    with patch('apps.core.url_security.requests.Session') as session:
        with pytest.raises(ValueError, match='публичный HTTPS endpoint'):
            adapter.fetch_changes(datetime(2024, 1, 1))

    session.assert_not_called()


def test_onec_adapters_reject_plain_http_before_transport():
    for adapter in (
        OneCHTTPAdapter(_xml_connection('http://1c.example.com/api')),
        OneCXMLAdapter(_xml_connection('http://1c.example.com/feed.xml')),
    ):
        module = (
            'apps.datasources.adapters.onec_http.request_public_http_url'
            if isinstance(adapter, OneCHTTPAdapter)
            else 'apps.datasources.adapters.onec_xml.request_public_http_url'
        )
        with patch(module) as request:
            with pytest.raises(ValueError, match='HTTPS'):
                adapter.fetch_changes(datetime(2024, 1, 1))
        request.assert_not_called()


def test_onec_http_rejects_unbounded_page_before_transport():
    adapter = OneCHTTPAdapter(_xml_connection('https://1c.example.com/api'))
    with patch(
        'apps.datasources.adapters.onec_http.request_public_http_url',
    ) as request:
        with pytest.raises(OneCHTTPValidationError, match='limit'):
            adapter.fetch_changes(datetime(2024, 1, 1), limit=501)
    request.assert_not_called()


def test_onec_serializer_requires_https_and_rejects_url_credentials():
    common = {
        'name': 'Warehouse',
        'type': '1c_xml',
        'is_active': True,
    }
    plain_http = DataSourceConnectionSerializer(data={
        **common,
        'credentials': {
            'url': 'http://1c.example.com/feed.xml',
            'user': 'alice',
            'password': 'secret',
        },
    })
    embedded = DataSourceConnectionSerializer(data={
        **common,
        'credentials': {
            'url': 'https://alice:secret@1c.example.com/feed.xml',
            'user': '',
            'password': '',
        },
    })

    assert plain_http.is_valid() is False
    assert 'HTTPS' in str(plain_http.errors['credentials'])
    assert embedded.is_valid() is False
    assert 'публичный HTTPS endpoint' in str(embedded.errors['credentials'])


def test_switching_csv_connection_to_onec_requires_new_credentials():
    current = SimpleNamespace(type='csv')
    serializer = DataSourceConnectionUpdateSerializer(
        current,
        data={
            'name': 'Warehouse',
            'type': '1c_http',
            'is_active': True,
        },
    )

    assert serializer.is_valid() is False
    assert 'credentials' in serializer.errors


def test_onec_xml_uses_bounded_same_origin_transport(settings):
    settings.DATASOURCE_XML_MAX_BYTES = 12345
    source_url = 'https://93.184.216.34/feed.xml'
    response = _xml_response(b'<Catalog/>')

    with patch(
        'apps.datasources.adapters.onec_xml.request_public_http_url',
        return_value=response,
    ) as request:
        assert OneCXMLAdapter(_xml_connection(source_url)).fetch_changes(
            datetime(2024, 1, 1),
        ) == []

    assert request.call_args.kwargs['auth'] == ('xml-user', 'xml-password')
    assert request.call_args.kwargs['max_response_bytes'] == 12345
    assert request.call_args.kwargs['redirect_policy'] == 'same-origin'


def test_onec_xml_rejects_dtd_and_entities():
    payload = (
        b'<!DOCTYPE Catalog [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'
        b'<Catalog><Item><Name>&secret;</Name></Item></Catalog>'
    )
    response = _xml_response(payload)

    with patch(
        'apps.datasources.adapters.onec_xml.request_public_http_url',
        return_value=response,
    ):
        with pytest.raises(OneCXMLValidationError, match='DTD'):
            OneCXMLAdapter(_xml_connection('https://93.184.216.34/feed.xml')).fetch_changes(
                datetime(2024, 1, 1),
            )


def test_onec_xml_stream_parser_applies_offset_and_limit():
    payload = (
        b'<Catalog>'
        b'<Item><Article>A0</Article></Item>'
        b'<Item><Article>A1</Article></Item>'
        b'<Item><Article>A2</Article></Item>'
        b'<Item><Article>A3</Article></Item>'
        b'</Catalog>'
    )

    result = _parse_items(payload, offset=1, limit=2)

    assert [item['article'] for item in result] == ['A1', 'A2']


def test_onec_xml_validates_items_beyond_requested_page(settings):
    settings.DATASOURCE_XML_MAX_ITEMS = 2
    payload = (
        b'<Catalog>'
        b'<Item><Article>A0</Article></Item>'
        b'<Item><Article>A1</Article></Item>'
        b'<Item><Article>A2</Article></Item>'
        b'</Catalog>'
    )

    with pytest.raises(OneCXMLValidationError, match='XML-позиций'):
        _parse_items(payload, offset=0, limit=1)


def test_onec_xml_node_and_text_limits_are_enforced(settings):
    payload = b'<Catalog><Item><Name>abcdef</Name></Item></Catalog>'
    settings.DATASOURCE_XML_MAX_NODES = 2
    with pytest.raises(OneCXMLValidationError, match='XML-элементов'):
        _parse_items(payload, offset=0, limit=1)

    settings.DATASOURCE_XML_MAX_NODES = 10
    settings.DATASOURCE_XML_MAX_TEXT_CHARS = 5
    with pytest.raises(OneCXMLValidationError, match='Объём текста'):
        _parse_items(payload, offset=0, limit=1)


def test_onec_xml_rejects_invalid_pagination():
    with pytest.raises(OneCXMLValidationError, match='limit'):
        _parse_items(b'<Catalog/>', offset=0, limit=0)
    with pytest.raises(OneCXMLValidationError, match='offset'):
        _parse_items(b'<Catalog/>', offset=-1, limit=1)
    with pytest.raises(OneCXMLValidationError, match='limit'):
        _parse_items(b'<Catalog/>', offset=0, limit=501)


def test_onec_xml_parses_valid_bounded_response():
    payload = (
        b'<Catalog><Item><UUID>u1</UUID><Article>A1</Article>'
        b'<Name>Part</Name><Price>10.50</Price><StockQty>3</StockQty>'
        b'</Item></Catalog>'
    )
    response = _xml_response(payload)

    with patch(
        'apps.datasources.adapters.onec_xml.request_public_http_url',
        return_value=response,
    ):
        result = OneCXMLAdapter(
            _xml_connection('https://93.184.216.34/feed.xml'),
        ).fetch_changes(datetime(2024, 1, 1))

    assert result == [{
        'uuid': 'u1',
        'article': 'A1',
        'name': 'Part',
        'brand': '',
        'price': '10.50',
        'stock_qty': 3,
        'category': '',
        'condition': 'new',
    }]


def test_datasource_expensive_endpoints_declare_dual_throttles():
    expected = {
        DataSourceTestView: (
            'datasource_test_principal',
            'datasource_test_tenant',
        ),
        DataSourceSyncView: (
            'datasource_sync_principal',
            'datasource_sync_tenant',
        ),
        CSVUploadView: (
            'datasource_upload_principal',
            'datasource_upload_tenant',
        ),
    }

    for view, scopes in expected.items():
        assert view.throttle_classes == [
            DataSourcePrincipalRateThrottle,
            DataSourceTenantRateThrottle,
        ]
        assert view.principal_throttle_scope == scopes[0]
        assert view.tenant_throttle_scope == scopes[1]


def test_datasource_throttle_enforces_configured_rate(settings, monkeypatch):
    cache = LocMemCache('datasource-throttle-test', {})
    monkeypatch.setattr(DataSourcePrincipalRateThrottle, 'cache', cache)
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        'DEFAULT_THROTTLE_RATES': {
            **settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'],
            'datasource_sync_principal': '1/min',
        },
    }
    view = SimpleNamespace(
        principal_throttle_scope='datasource_sync_principal',
        expensive_throttle_methods={'POST'},
    )

    assert DataSourcePrincipalRateThrottle().allow_request(_request(), view) is True
    assert DataSourcePrincipalRateThrottle().allow_request(_request(), view) is False
