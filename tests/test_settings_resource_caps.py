import os
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _memory_mib(value: str) -> int:
    normalized = value.strip().lower()
    assert normalized.endswith('m')
    return int(normalized[:-1])


def test_production_compose_fits_the_supported_four_gib_host_budget():
    compose = yaml.safe_load((ROOT / 'docker-compose.prod.yml').read_text())
    services = compose['services']
    normal_names = {
        'django', 'celery_worker', 'celery_beat', 'celery_worker_images',
        'frontend', 'db', 'redis', 'redis_broker', 'egress_proxy', 'nginx',
    }
    normal_budget = sum(
        _memory_mib(services[name]['mem_limit']) for name in normal_names
    )
    backup_budget = _memory_mib(services['backup']['mem_limit'])

    # Leave about 584 MiB of a 3.7 GiB CPX22 for the kernel, Docker and
    # deployment overhead, even while the bounded backup container is active.
    assert normal_budget <= 2816
    assert backup_budget <= 384
    assert normal_budget + backup_budget <= 3200
    assert '--maxmemory 160mb' in ' '.join(services['redis']['command'])
    assert '--maxmemory 224mb' in ' '.join(services['redis_broker']['command'])


def test_capacity_gate_reserves_memory_for_sequential_release_builds():
    capacity = (ROOT / 'scripts' / 'check_production_capacity.sh').read_text()
    assert 'MIN_TOTAL_MEMORY_KB=3670016' in capacity
    assert 'MIN_AVAILABLE_MEMORY_KB=1048576' in capacity


def test_capacity_gate_avoids_gawk_reserved_load_builtin():
    capacity = (ROOT / 'scripts' / 'check_production_capacity.sh').read_text()
    assert '-v load=' not in capacity
    assert '-v one_minute_load="$load_one"' in capacity
    assert 'one_minute_load <= cpu_total * max_per_cpu' in capacity


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
        'PRODUCT_PARSE_TENANT_DAILY_JOBS': oversized,
        'WEB_RESEARCH_MAX_QUERIES': oversized,
        'WEB_RESEARCH_RESULTS_PER_QUERY': oversized,
        'WEB_SEARCH_STARTED_STALE_SECONDS': '-100',
        'WEB_SEARCH_CHECKPOINT_MAX_BYTES': oversized,
        'WEB_SEARCH_WORKFLOW_INPUT_MAX_BYTES': oversized,
        'YOOKASSA_API_CONNECT_TIMEOUT_SECONDS': oversized,
        'YOOKASSA_API_READ_TIMEOUT_SECONDS': oversized,
        'YOOKASSA_API_MAX_ELAPSED_SECONDS': oversized,
        'YOOKASSA_WEBHOOK_PROCESSING_TIMEOUT_SECONDS': oversized,
        'BILLING_OUTBOX_PROCESSING_TIMEOUT_SECONDS': oversized,
        'SOFT_DELETE_RETENTION_DAYS': '-100',
        'WEBHOOK_AUDIT_RETENTION_DAYS': '-100',
        'BILLING_AUDIT_RETENTION_DAYS': '-100',
        'SYNC_LOG_RETENTION_DAYS': '-100',
        'PRODUCT_PARSE_RAW_HTML_RETENTION_DAYS': '-100',
        'PRODUCT_PARSE_JOB_RETENTION_DAYS': '-100',
        'IMAGE_SEARCH_LOG_RETENTION_DAYS': '-100',
        'IMAGE_SEARCH_TASK_RETENTION_DAYS': '-100',
        'PRODUCT_BULK_ACTION_JOB_RETENTION_DAYS': '-100',
        'MEDIA_PROCESSING_JOB_RETENTION_DAYS': '-100',
        'BACKGROUND_JOB_RETENTION_DAYS': '-100',
        'WEB_SEARCH_ATTEMPT_RETENTION_DAYS': '-100',
        'RETENTION_PURGE_BATCH_SIZE': oversized,
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
assert s.PRODUCT_PARSE_TENANT_DAILY_JOBS == 1000
assert s.WEB_RESEARCH_MAX_QUERIES == 10
assert s.WEB_RESEARCH_RESULTS_PER_QUERY == 20
assert s.WEB_SEARCH_STARTED_STALE_SECONDS == 3700
assert s.WEB_SEARCH_CHECKPOINT_MAX_BYTES == 4 * 1024 * 1024
assert s.WEB_SEARCH_WORKFLOW_INPUT_MAX_BYTES == 512 * 1024
assert s.YOOKASSA_API_CONNECT_TIMEOUT_SECONDS == 30
assert s.YOOKASSA_API_READ_TIMEOUT_SECONDS == 60
assert s.YOOKASSA_API_MAX_ELAPSED_SECONDS == 120
assert s.YOOKASSA_WEBHOOK_PROCESSING_TIMEOUT_SECONDS == 3600
assert s.BILLING_OUTBOX_PROCESSING_TIMEOUT_SECONDS == 3600
assert s.SOFT_DELETE_RETENTION_DAYS == 1
assert s.WEBHOOK_AUDIT_RETENTION_DAYS == 1
assert s.BILLING_AUDIT_RETENTION_DAYS == 1
assert s.SYNC_LOG_RETENTION_DAYS == 1
assert s.PRODUCT_PARSE_RAW_HTML_RETENTION_DAYS == 1
assert s.PRODUCT_PARSE_JOB_RETENTION_DAYS == 1
assert s.IMAGE_SEARCH_LOG_RETENTION_DAYS == 1
assert s.IMAGE_SEARCH_TASK_RETENTION_DAYS == 1
assert s.PRODUCT_BULK_ACTION_JOB_RETENTION_DAYS == 1
assert s.MEDIA_PROCESSING_JOB_RETENTION_DAYS == 1
assert s.BACKGROUND_JOB_RETENTION_DAYS == 1
assert s.WEB_SEARCH_ATTEMPT_RETENTION_DAYS == 30
assert s.RETENTION_PURGE_BATCH_SIZE == 10000
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
