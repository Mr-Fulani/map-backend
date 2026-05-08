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
