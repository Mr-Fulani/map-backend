import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from apps.datasources.adapters.csv_adapter import CSVAdapter, CSVValidationError
from apps.datasources.adapters.onec_http import OneCHTTPAdapter
from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.datasources.services import ConnectionService
from apps.tenants.services import TenantService

BASE_URL = 'https://1c.example.com'
CREDS = {'url': BASE_URL, 'user': 'admin', 'password': 'secret'}


def make_connection(url=BASE_URL):
    conn = MagicMock()
    conn.credentials = encrypt({**CREDS, 'url': url})
    conn.type = '1c_http'
    return conn


@pytest.mark.django_db
class TestOneCHTTPAdapter:
    def test_fetch_changes_uses_bounded_same_origin_transport(self, settings):
        settings.DATASOURCE_HTTP_MAX_BYTES = 12345
        response = MagicMock()
        response.json.return_value = {
            'items': [{
                'uuid': 'abc',
                'article': 'A100',
                'name': 'Деталь',
                'price': '1000',
                'stock_qty': 5,
            }],
        }

        with patch(
            'apps.datasources.adapters.onec_http.request_public_http_url',
            return_value=response,
        ) as request:
            items = OneCHTTPAdapter(make_connection()).fetch_changes(
                since=datetime(2024, 1, 1),
            )

        assert len(items) == 1
        assert items[0]['article'] == 'A100'
        assert request.call_args.kwargs['auth'] == ('admin', 'secret')
        assert request.call_args.kwargs['max_response_bytes'] == 12345
        assert request.call_args.kwargs['redirect_policy'] == 'same-origin'

    def test_timeout_raises_exception(self):
        with patch(
            'apps.datasources.adapters.onec_http.request_public_http_url',
            side_effect=requests.exceptions.ConnectTimeout(),
        ), pytest.raises(requests.exceptions.ConnectTimeout):
            OneCHTTPAdapter(make_connection()).fetch_changes(
                since=datetime(2024, 1, 1),
            )

    def test_test_connection_returns_true(self):
        response = MagicMock()
        with patch(
            'apps.datasources.adapters.onec_http.request_public_http_url',
            return_value=response,
        ) as request:
            assert OneCHTTPAdapter(make_connection()).test_connection() is True

        assert request.call_args.kwargs['status_only'] is True
        assert request.call_args.kwargs['max_response_bytes'] is None

    def test_private_url_is_rejected_before_transport(self):
        with patch('apps.core.url_security.requests.Session') as session:
            with pytest.raises(ValueError, match='публичн'):
                OneCHTTPAdapter(
                    make_connection('https://127.0.0.1:8080'),
                ).fetch_changes(datetime(2024, 1, 1))

        session.assert_not_called()

    def test_source_cannot_return_more_than_requested_limit(self):
        response = MagicMock()
        response.json.return_value = {'items': [{}, {}]}
        with patch(
            'apps.datasources.adapters.onec_http.request_public_http_url',
            return_value=response,
        ), pytest.raises(ValueError, match='лимит строк'):
            OneCHTTPAdapter(make_connection()).fetch_changes(
                datetime(2024, 1, 1),
                limit=1,
            )


class TestCSVAdapter:
    def _write_csv(self, content: str, suffix='.csv') -> str:
        f = tempfile.NamedTemporaryFile(mode='w', suffix=suffix, delete=False, encoding='utf-8')
        f.write(content)
        f.close()
        return f.name

    def _write_xlsx(self, rows: list[list]) -> str:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        for row in rows:
            ws.append(row)
        f = tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False)
        f.close()
        wb.save(f.name)
        return f.name

    def _write_xls(self, rows: list[list]) -> str:
        import xlwt
        wb = xlwt.Workbook()
        ws = wb.add_sheet('Sheet1')
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                ws.write(r, c, value)
        f = tempfile.NamedTemporaryFile(suffix='.xls', delete=False)
        f.close()
        wb.save(f.name)
        return f.name

    def test_xls_parsing(self):
        # Старый формат Excel 97-2003 — был источником 500 (openpyxl его не читает).
        path = self._write_xls([
            ['article', 'name', 'price', 'stock_qty'],
            [12345, 'Болт', 99.9, 50],
        ])
        try:
            adapter = CSVAdapter(connection=None)
            items = adapter.process_uploaded_file(path)
            assert len(items) == 1
            assert items[0]['article'] == '12345'  # float→int→str, не '12345.0'
            assert items[0]['stock_qty'] == 50
        finally:
            os.unlink(path)

    def test_csv_cp1251_encoding(self):
        f = tempfile.NamedTemporaryFile(mode='wb', suffix='.csv', delete=False)
        f.write('article,name,price,stock_qty\nA100,Деталь,1500,10\n'.encode('cp1251'))
        f.close()
        try:
            adapter = CSVAdapter(connection=None)
            items = adapter.process_uploaded_file(f.name)
            assert items[0]['name'] == 'Деталь'
        finally:
            os.unlink(f.name)

    def test_csv_semicolon_delimiter(self):
        path = self._write_csv('article;name;price;stock_qty\nA100;Деталь;1500;10\n')
        try:
            adapter = CSVAdapter(connection=None)
            items = adapter.process_uploaded_file(path)
            assert len(items) == 1
            assert items[0]['article'] == 'A100'
        finally:
            os.unlink(path)

    def test_unsupported_format_raises_clear_error(self):
        path = self._write_csv('мусор', suffix='.pdf')
        try:
            adapter = CSVAdapter(connection=None)
            with pytest.raises(CSVValidationError, match='не поддерживается'):
                adapter.process_uploaded_file(path)
        finally:
            os.unlink(path)

    def test_valid_csv_parsed_correctly(self):
        path = self._write_csv('article,name,price,stock_qty\nA100,Деталь,1500.00,10\n')
        try:
            adapter = CSVAdapter(connection=None)
            items = adapter.process_uploaded_file(path)
            assert len(items) == 1
            assert items[0]['article'] == 'A100'
            assert items[0]['stock_qty'] == 10
            assert items[0]['price'] == '1500.00'
        finally:
            os.unlink(path)

    def test_missing_required_column_raises_error(self):
        path = self._write_csv('article,name,price\nA100,Деталь,1500\n')
        try:
            adapter = CSVAdapter(connection=None)
            with pytest.raises(CSVValidationError, match='stock_qty'):
                adapter.process_uploaded_file(path)
        finally:
            os.unlink(path)

    def test_xlsx_parsing(self):
        path = self._write_xlsx([
            ['article', 'name', 'price', 'stock_qty'],
            ['B200', 'Болт', '99.90', '50'],
        ])
        try:
            adapter = CSVAdapter(connection=None)
            items = adapter.process_uploaded_file(path)
            assert len(items) == 1
            assert items[0]['article'] == 'B200'
            assert items[0]['stock_qty'] == 50
        finally:
            os.unlink(path)

    def test_xlsx_1c_report_with_two_row_header(self):
        path = self._write_xlsx([
            ['В отчет выведены результаты предварительного закрытия месяца.', None, None, None, None],
            [None, None, None, None, None],
            ['Себестоимость товаров предприятия', None, None, None, None],
            [None, None, None, None, None],
            ['Номенклатура.Производитель', 'Артикул', 'Номенклатура', 'Конечный остаток', None],
            [None, None, None, 'Количество', 'Средняя цена'],
            ['HYUNDAI/KIA/MOBIS', '98620H5500', 'БАЧОК СТЕКЛООМЫВАТЕЛЯ', 3, 3657.63],
            [None, '28210H5100', 'Бачок воздухозаборника', 2, 1435.82],
        ])
        try:
            adapter = CSVAdapter(connection=None)
            items = adapter.process_uploaded_file(path)
            assert len(items) == 2
            assert items[0]['article'] == '98620H5500'
            assert items[0]['name'] == 'БАЧОК СТЕКЛООМЫВАТЕЛЯ'
            assert items[0]['brand'] == 'HYUNDAI/KIA/MOBIS'
            assert items[0]['stock_qty'] == 3
            assert items[0]['price'] == '3657.63'
            assert items[1]['brand'] == 'HYUNDAI/KIA/MOBIS'
        finally:
            os.unlink(path)

    def test_preview_returns_correct_structure(self):
        path = self._write_csv('article,name,price,stock_qty\n' + 'A,B,1,1\n' * 15)
        try:
            adapter = CSVAdapter(connection=None)
            preview = adapter.preview(path, rows=10)
            assert 'headers' in preview
            assert 'rows' in preview
            assert len(preview['rows']) == 10
            assert preview['total_rows'] == 15
        finally:
            os.unlink(path)

    def test_invalid_price_raises_error(self):
        path = self._write_csv('article,name,price,stock_qty\nA100,Деталь,abc,10\n')
        try:
            adapter = CSVAdapter(connection=None)
            with pytest.raises(CSVValidationError, match='цена'):
                adapter.process_uploaded_file(path)
        finally:
            os.unlink(path)

    @pytest.mark.django_db
    def test_csv_upload_creates_separate_datasource_connections(self):
        tenant, _ = TenantService.create_tenant('csv-multi', 'csv-multi', 'csv@test.com', 'pass12345')
        items = [{'article': 'A100', 'name': 'Деталь', 'price': '100', 'stock_qty': 1}]

        first = ConnectionService.process_csv_upload(tenant, 'first.xlsx', items)
        second = ConnectionService.process_csv_upload(tenant, 'second.xlsx', items)

        assert first['id'] != second['id']
        assert DataSourceConnection.objects.filter(
            tenant=tenant,
            type=DataSourceConnection.TYPE_CSV,
        ).count() == 2

    @pytest.mark.django_db
    def test_find_duplicate_upload_by_hash(self):
        tenant, _ = TenantService.create_tenant('csv-hash', 'csv-hash', 'hash@test.com', 'pass12345')
        items = [{'article': 'A100', 'name': 'Деталь', 'price': '100', 'stock_qty': 1}]
        ConnectionService.process_csv_upload(tenant, 'stock.xlsx', items, content_hash='abc123')

        dup = ConnectionService.find_duplicate_upload(tenant, 'abc123', 'other-name.xlsx')
        assert dup is not None
        assert dup['reason'] == 'hash'

    @pytest.mark.django_db
    def test_find_duplicate_upload_by_name(self):
        tenant, _ = TenantService.create_tenant('csv-name', 'csv-name', 'name@test.com', 'pass12345')
        items = [{'article': 'A100', 'name': 'Деталь', 'price': '100', 'stock_qty': 1}]
        ConnectionService.process_csv_upload(tenant, 'stock.xlsx', items, content_hash='hash-1')

        dup = ConnectionService.find_duplicate_upload(tenant, 'hash-2', 'stock.xlsx')
        assert dup is not None
        assert dup['reason'] == 'name'

    @pytest.mark.django_db
    def test_find_duplicate_upload_none_for_new_file(self):
        tenant, _ = TenantService.create_tenant('csv-new', 'csv-new', 'new@test.com', 'pass12345')
        items = [{'article': 'A100', 'name': 'Деталь', 'price': '100', 'stock_qty': 1}]
        ConnectionService.process_csv_upload(tenant, 'stock.xlsx', items, content_hash='hash-1')

        assert ConnectionService.find_duplicate_upload(tenant, 'hash-2', 'fresh.xlsx') is None
