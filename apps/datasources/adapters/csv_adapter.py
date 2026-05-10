import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path

import openpyxl

from apps.datasources.base import BaseDataSourceAdapter


class CSVValidationError(ValueError):
    pass


class CSVAdapter(BaseDataSourceAdapter):
    REQUIRED_COLUMNS = ['article', 'name', 'price', 'stock_qty']
    OPTIONAL_COLUMNS = ['brand', 'category', 'condition', 'oem_numbers', 'cross_numbers', 'description']

    def fetch_changes(self, since=None, limit=500, offset=0) -> list[dict]:
        raise NotImplementedError('CSVAdapter использует process_uploaded_file()')

    def test_connection(self) -> bool:
        return True

    def get_display_name(self) -> str:
        return 'CSV/Excel'

    def process_uploaded_file(self, file_path: str) -> list[dict]:
        path = Path(file_path)
        if path.suffix.lower() in ('.xlsx', '.xls'):
            rows = self._read_xlsx(file_path)
        else:
            rows = self._read_csv(file_path)
        return self._normalize(rows)

    def preview(self, file_path: str, rows: int = 10) -> dict:
        path = Path(file_path)
        if path.suffix.lower() in ('.xlsx', '.xls'):
            all_rows = self._read_xlsx(file_path)
        else:
            all_rows = self._read_csv(file_path)
        headers = list(all_rows[0].keys()) if all_rows else []
        return {
            'headers': headers,
            'rows': all_rows[:rows],
            'total_rows': len(all_rows),
        }

    def _read_csv(self, file_path: str) -> list[dict]:
        with open(file_path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            return [row for row in reader]

    def _read_xlsx(self, file_path: str) -> list[dict]:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(h).strip() if h is not None else '' for h in rows[0]]
        result = []
        for row in rows[1:]:
            result.append({headers[i]: (str(v).strip() if v is not None else '') for i, v in enumerate(row)})
        wb.close()
        return result

    COLUMN_ALIASES = {
        'артикул': 'article',
        'номер производ.': 'article',
        'номер производителя': 'article',
        'название': 'name',
        'наименование': 'name',
        'номенклатура': 'name',
        'товар': 'name',
        'цена': 'price',
        'стоимость': 'price',
        'остаток': 'stock_qty',
        'количество': 'stock_qty',
        'кол-во': 'stock_qty',
        'бренд': 'brand',
        'производитель': 'brand',
        'марка': 'brand',
        'категория': 'category',
        'состояние': 'condition',
        'oem': 'oem_numbers',
        'кроссы': 'cross_numbers',
        'кросс-номера': 'cross_numbers',
        'описание': 'description',
    }

    def _normalize(self, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
            
        headers = {k.lower().strip() for k in rows[0].keys() if k}
        
        # Переименовываем колонки по алиасам для каждой строки
        normalized_rows = []
        for row in rows:
            mapped_row = {}
            for k, v in row.items():
                if k is None:
                    continue
                k_lower = str(k).lower().strip()
                # Применяем алиас, если есть
                mapped_key = self.COLUMN_ALIASES.get(k_lower, k_lower)
                mapped_row[mapped_key] = v
            normalized_rows.append(mapped_row)
            
        mapped_headers = set(normalized_rows[0].keys()) if normalized_rows else set()
        missing = [col for col in self.REQUIRED_COLUMNS if col not in mapped_headers]
        if missing:
            raise CSVValidationError(f'Отсутствуют обязательные колонки: {", ".join(missing)}')

        result = []
        for i, row in enumerate(normalized_rows, start=2):
            normalized = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            
            # Если цена пустая, пропускаем или ставим 0? Поставим 0 или выкинем ошибку
            price_val = str(normalized.get('price', 0)).replace(',', '.').replace(' ', '')
            try:
                price = Decimal(price_val)
            except InvalidOperation:
                raise CSVValidationError(f'Строка {i}: некорректная цена "{normalized["price"]}"')
            try:
                stock_val = str(normalized.get('stock_qty', 0)).replace(',', '.').replace(' ', '')
                stock_qty = int(float(stock_val))
            except (ValueError, TypeError):
                raise CSVValidationError(f'Строка {i}: некорректное количество "{normalized["stock_qty"]}"')

            result.append({
                'uuid': None,
                'article': normalized['article'],
                'name': normalized['name'],
                'brand': normalized.get('brand', ''),
                'price': str(price),
                'stock_qty': stock_qty,
                'category': normalized.get('category', ''),
                'condition': normalized.get('condition', 'new'),
                'oem_numbers': normalized.get('oem_numbers', ''),
                'cross_numbers': normalized.get('cross_numbers', ''),
                'description': normalized.get('description', ''),
            })
        return result
