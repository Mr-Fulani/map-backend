import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_hostile_environment_cannot_raise_resource_hard_ceilings():
    oversized = str(10 ** 12)
    env = {
        **os.environ,
        'PASSWORD_RESET_TIMEOUT': oversized,
        'API_REQUEST_MAX_BYTES': oversized,
        'FILE_UPLOAD_MEMORY_MAX_BYTES': oversized,
        'DATA_UPLOAD_MAX_NUMBER_FIELDS': oversized,
        'WEBHOOK_REQUEST_TIMEOUT_SECONDS': oversized,
        'WEBHOOK_MAX_ATTEMPTS': oversized,
        'MAX_IMAGE_UPLOAD_BYTES': oversized,
        'MAX_DECODED_IMAGE_PIXELS': oversized,
        'MEDIA_PROVIDER_OUTPUT_MAX_BYTES': oversized,
        'API_BULK_MAX_ITEMS': oversized,
        'DATASOURCE_UPLOAD_MAX_BYTES': oversized,
        'DATASOURCE_XLSX_MAX_UNCOMPRESSED_BYTES': oversized,
        'DATASOURCE_XLSX_MAX_ARCHIVE_ENTRIES': oversized,
        'DATASOURCE_IMPORT_MAX_ROWS': oversized,
        'DATASOURCE_IMPORT_MAX_COLUMNS': oversized,
        'DATASOURCE_IMPORT_MAX_CELLS': oversized,
        'DATASOURCE_XML_MAX_BYTES': oversized,
        'DATASOURCE_HTTP_MAX_BYTES': oversized,
        'DATASOURCE_XML_MAX_NODES': oversized,
        'DATASOURCE_XML_MAX_TEXT_CHARS': oversized,
        'DATASOURCE_XML_MAX_ITEMS': oversized,
        'DATASOURCE_FETCH_PAGE_MAX_ITEMS': oversized,
        'PART_PAGE_MAX_BYTES': oversized,
        'AVITO_API_RESPONSE_MAX_BYTES': oversized,
        'TRUSTED_API_RESPONSE_MAX_BYTES': oversized,
        'IMAGE_SEARCH_BULK_MAX_PRODUCTS': oversized,
        'WEB_RESEARCH_MAX_QUERIES': oversized,
        'WEB_RESEARCH_RESULTS_PER_QUERY': oversized,
        'YOOKASSA_API_CONNECT_TIMEOUT_SECONDS': oversized,
        'YOOKASSA_API_READ_TIMEOUT_SECONDS': oversized,
        'YOOKASSA_API_MAX_ELAPSED_SECONDS': oversized,
        'YOOKASSA_WEBHOOK_PROCESSING_TIMEOUT_SECONDS': oversized,
        'BILLING_OUTBOX_PROCESSING_TIMEOUT_SECONDS': oversized,
        'SOFT_DELETE_RETENTION_DAYS': '-100',
        'WEBHOOK_AUDIT_RETENTION_DAYS': '-100',
        'BILLING_AUDIT_RETENTION_DAYS': '-100',
        'SYNC_LOG_RETENTION_DAYS': '-100',
    }
    assertions = """
from config.settings import base as s
assert s.PASSWORD_RESET_TIMEOUT == 86400
assert s.DATA_UPLOAD_MAX_MEMORY_SIZE == 16 * 1024 * 1024
assert s.FILE_UPLOAD_MAX_MEMORY_SIZE == 2 * 1024 * 1024
assert s.DATA_UPLOAD_MAX_NUMBER_FIELDS == 5000
assert s.WEBHOOK_REQUEST_TIMEOUT_SECONDS == 30
assert s.WEBHOOK_MAX_ATTEMPTS == 20
assert s.MAX_IMAGE_UPLOAD_BYTES == 5 * 1024 * 1024
assert s.MAX_DECODED_IMAGE_PIXELS == 16_000_000
assert s.MEDIA_PROVIDER_OUTPUT_MAX_BYTES == 25 * 1024 * 1024
assert s.API_BULK_MAX_ITEMS == 500
assert s.DATASOURCE_UPLOAD_MAX_BYTES == 5 * 1024 * 1024
assert s.DATASOURCE_XLSX_MAX_UNCOMPRESSED_BYTES == 25 * 1024 * 1024
assert s.DATASOURCE_XLSX_MAX_ARCHIVE_ENTRIES == 1024
assert s.DATASOURCE_IMPORT_MAX_ROWS == 5000
assert s.DATASOURCE_IMPORT_MAX_COLUMNS == 128
assert s.DATASOURCE_IMPORT_MAX_CELLS == 100_000
assert s.DATASOURCE_XML_MAX_BYTES == 8 * 1024 * 1024
assert s.DATASOURCE_HTTP_MAX_BYTES == 5 * 1024 * 1024
assert s.DATASOURCE_XML_MAX_NODES == 60_000
assert s.DATASOURCE_XML_MAX_TEXT_CHARS == 4 * 1024 * 1024
assert s.DATASOURCE_XML_MAX_ITEMS == 5000
assert s.DATASOURCE_FETCH_PAGE_MAX_ITEMS == 500
assert s.PART_PAGE_MAX_BYTES == 2 * 1024 * 1024
assert s.AVITO_API_RESPONSE_MAX_BYTES == 5 * 1024 * 1024
assert s.TRUSTED_API_RESPONSE_MAX_BYTES == 5 * 1024 * 1024
assert s.IMAGE_SEARCH_BULK_MAX_PRODUCTS == 25
assert s.WEB_RESEARCH_MAX_QUERIES == 10
assert s.WEB_RESEARCH_RESULTS_PER_QUERY == 20
assert s.YOOKASSA_API_CONNECT_TIMEOUT_SECONDS == 30
assert s.YOOKASSA_API_READ_TIMEOUT_SECONDS == 60
assert s.YOOKASSA_API_MAX_ELAPSED_SECONDS == 120
assert s.YOOKASSA_WEBHOOK_PROCESSING_TIMEOUT_SECONDS == 3600
assert s.BILLING_OUTBOX_PROCESSING_TIMEOUT_SECONDS == 3600
assert s.SOFT_DELETE_RETENTION_DAYS == 1
assert s.WEBHOOK_AUDIT_RETENTION_DAYS == 1
assert s.BILLING_AUDIT_RETENTION_DAYS == 1
assert s.SYNC_LOG_RETENTION_DAYS == 1
"""

    result = subprocess.run(
        [sys.executable, '-c', assertions],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
