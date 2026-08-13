from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Event
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.db import close_old_connections

from apps.datasources.encryption import decrypt
from apps.datasources.encryption import encrypt
from apps.media_processing.models import MediaProcessingJob
from apps.media_processing.providers.base import (
    MediaProviderResult,
    MediaProviderResultStatus,
)
from apps.media_processing.services import _checkpoint_provider_result
from apps.products.models import Product, ProductImage
from apps.tenants.services import TenantService
from apps.web_research.accounting import (
    acquire_web_search_workflow, deterministic_web_search_call_key,
    execute_recorded_web_search, fingerprint_web_search_request,
)
from apps.web_research.models import WebSearchAttempt, WebSearchConnection


@pytest.mark.django_db
def test_rotation_includes_web_search_credentials_and_dry_run_is_read_only(settings):
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    settings.FIELD_ENCRYPTION_KEY = old_key
    settings.FIELD_ENCRYPTION_KEYS = [old_key]
    connection = WebSearchConnection(
        provider_id='rotation-test',
        display_name='Rotation test',
    )
    connection.set_credentials({'api_key': 'secret-value'})
    connection.save()
    tenant, _ = TenantService.create_tenant(
        'Checkpoint rotation',
        'checkpoint-rotation',
        'checkpoint-rotation@example.com',
        'pass12345',
    )
    product = Product.objects.create(
        tenant=tenant,
        article='ROTATE-1',
        name='Checkpoint rotation product',
        price='1.00',
    )
    image = ProductImage.objects.create(
        product=product,
        s3_key='products/rotation/source.jpg',
    )
    media_job = MediaProcessingJob.objects.create(
        tenant=tenant,
        product_image=image,
        operations=['resize'],
    )
    _checkpoint_provider_result(media_job, MediaProviderResult(
        status=MediaProviderResultStatus.PENDING,
        provider_job_id='rotation-provider-job',
    ))
    workflow = acquire_web_search_workflow(
        tenant=tenant,
        product=product,
        operation='image_search',
        domain_reference=f'product:{tenant.pk}:{product.pk}',
        workflow_key='image-search-task:rotation',
        input_snapshot={'query': 'rotation image'},
    )
    request_fingerprint = fingerprint_web_search_request({
        'query': 'rotation image', 'count': 1,
    })
    execute_recorded_web_search(
        workflow=workflow,
        provider=type('Provider', (), {'provider_id': 'brave'})(),
        query='rotation image',
        call_key=deterministic_web_search_call_key(
            provider_id='brave', call_kind='image', slot='query:0',
        ),
        request_fingerprint=request_fingerprint,
        call=lambda: [{'url': 'https://example.com/image.jpg'}],
        call_kind='image',
    )
    old_ciphertext = bytes(connection.credentials_enc)
    media_job.refresh_from_db()
    old_checkpoint = bytes(media_job.provider_response_enc)
    web_attempt = WebSearchAttempt.objects.get(workflow=workflow)
    old_web_checkpoint = bytes(web_attempt.checkpoint_enc)

    settings.FIELD_ENCRYPTION_KEY = new_key
    settings.FIELD_ENCRYPTION_KEYS = [new_key, old_key]
    output = StringIO()
    call_command('rotate_encryption_keys', '--dry-run', stdout=output)

    connection.refresh_from_db()
    media_job.refresh_from_db()
    web_attempt.refresh_from_db()
    assert bytes(connection.credentials_enc) == old_ciphertext
    assert bytes(media_job.provider_response_enc) == old_checkpoint
    assert bytes(web_attempt.checkpoint_enc) == old_web_checkpoint
    assert '[dry-run] web_search_connections: 1' in output.getvalue()
    assert '[dry-run] media_provider_checkpoints: 1' in output.getvalue()
    assert '[dry-run] web_search_checkpoints: 1' in output.getvalue()

    call_command('rotate_encryption_keys', stdout=StringIO())

    connection.refresh_from_db()
    media_job.refresh_from_db()
    web_attempt.refresh_from_db()
    assert bytes(connection.credentials_enc) != old_ciphertext
    assert bytes(media_job.provider_response_enc) != old_checkpoint
    assert bytes(web_attempt.checkpoint_enc) != old_web_checkpoint
    settings.FIELD_ENCRYPTION_KEYS = [new_key]
    assert decrypt(connection.credentials_enc) == {'api_key': 'secret-value'}
    assert decrypt(media_job.provider_response_enc)['provider_job_id'] == (
        'rotation-provider-job'
    )
    assert decrypt(web_attempt.checkpoint_enc)['result'] == [
        {'url': 'https://example.com/image.jpg'},
    ]


@pytest.mark.django_db(transaction=True)
def test_rotation_row_lock_serializes_concurrent_checkpoint_update(settings):
    key = Fernet.generate_key().decode()
    settings.FIELD_ENCRYPTION_KEY = key
    settings.FIELD_ENCRYPTION_KEYS = [key]
    tenant, _ = TenantService.create_tenant(
        'Concurrent checkpoint rotation',
        'concurrent-checkpoint-rotation',
        'concurrent-checkpoint-rotation@example.com',
        'pass12345',
    )
    workflow = acquire_web_search_workflow(
        tenant=tenant,
        operation='image_search',
        domain_reference='product:rotation-concurrency',
        workflow_key='image-search-task:rotation-concurrency',
        input_snapshot={'query': 'rotation lock'},
    )
    execute_recorded_web_search(
        workflow=workflow,
        provider=type('Provider', (), {'provider_id': 'brave'})(),
        query='rotation lock',
        call_key=deterministic_web_search_call_key(
            provider_id='brave', call_kind='image', slot='query:0',
        ),
        request_fingerprint=fingerprint_web_search_request({
            'query': 'rotation lock',
        }),
        call=lambda: [{'url': 'https://example.com/original.jpg'}],
        call_kind='image',
    )
    attempt = WebSearchAttempt.objects.get(workflow=workflow)
    writer_ciphertext = encrypt({
        'version': 1,
        'result': [{'url': 'https://example.com/concurrent-writer.jpg'}],
    })
    row_locked = Event()
    allow_rotation = Event()
    original_decrypt = decrypt

    def blocking_decrypt(value):
        row_locked.set()
        assert allow_rotation.wait(timeout=10)
        return original_decrypt(value)

    def rotate():
        close_old_connections()
        try:
            call_command('rotate_encryption_keys', stdout=StringIO())
        finally:
            close_old_connections()

    def concurrent_update():
        close_old_connections()
        try:
            assert row_locked.wait(timeout=10)
            WebSearchAttempt.objects.filter(pk=attempt.pk).update(
                checkpoint_enc=writer_ciphertext,
            )
        finally:
            close_old_connections()

    with patch(
        'apps.core.management.commands.rotate_encryption_keys.decrypt',
        side_effect=blocking_decrypt,
    ), ThreadPoolExecutor(max_workers=2) as pool:
        rotation_future = pool.submit(rotate)
        assert row_locked.wait(timeout=10)
        writer_future = pool.submit(concurrent_update)
        allow_rotation.set()
        rotation_future.result(timeout=15)
        writer_future.result(timeout=15)

    attempt.refresh_from_db()
    assert decrypt(bytes(attempt.checkpoint_enc))['result'] == [
        {'url': 'https://example.com/concurrent-writer.jpg'},
    ]
