import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock

import pytest
import requests
import responses as responses_lib

from apps.datasources.adapters.csv_adapter import CSVAdapter, CSVValidationError
from apps.datasources.adapters.onec_http import OneCHTTPAdapter
from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.datasources.services import ConnectionService
from apps.tenants.services import TenantService

BASE_URL = 'http://1c.example.com'
CREDS = {'url': BASE_URL, 'user': 'admin', 'password': 'secret'}


def make_connection():
    conn = MagicMock()
    conn.credentials = encrypt(CREDS)
    conn.type = '1c_http'
    return conn


@pytest.mark.django_db
class TestOneCHTTPAdapter:
    @responses_lib.activate
    def test_fetch_changes_returns_normalized_format(self):
        responses_lib.add(
            responses_lib.GET,
            f'{BASE_URL}/avito-sync/changes',
            json={'items': [{'uuid': 'abc', 'article': 'A100', 'name': 'Деталь', 'price': '1000', 'stock_qty': 5}]},
            status=200,
        )
        adapter = OneCHTTPAdapter(make_connection())
        items = adapter.fetch_changes(since=datetime(2024, 1, 1))
        assert len(items) == 1
        assert items[0]['article'] == 'A100'

    @responses_lib.activate
    def test_retry_on_5xx(self):
        responses_lib.add(responses_lib.GET, f'{BASE_URL}/avito-sync/changes', status=503)
        responses_lib.add(responses_lib.GET, f'{BASE_URL}/avito-sync/changes', status=503)
        responses_lib.add(
            responses_lib.GET,
            f'{BASE_URL}/avito-sync/changes',
            json={'items': []},
            status=200,
        )
        adapter = OneCHTTPAdapter(make_connection())
        items = adapter.fetch_changes(since=datetime(2024, 1, 1))
        assert items == []

    @responses_lib.activate
    def test_timeout_raises_exception(self):
        responses_lib.add(
            responses_lib.GET,
            f'{BASE_URL}/avito-sync/changes',
            body=requests.exceptions.ConnectTimeout(),
        )
        adapter = OneCHTTPAdapter(make_connection())
        with pytest.raises(requests.exceptions.ConnectionError):
            adapter.fetch_changes(since=datetime(2024, 1, 1))

    @responses_lib.activate
    def test_test_connection_returns_true(self):
        responses_lib.add(
            responses_lib.GET,
            f'{BASE_URL}/avito-sync/changes',
            json={'items': []},
            status=200,
        )
        adapter = OneCHTTPAdapter(make_connection())
        assert adapter.test_connection() is True


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
