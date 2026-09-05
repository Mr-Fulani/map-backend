"""Тесты API управления изображениями товаров."""

import io
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache.backends.locmem import LocMemCache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connections
from django.test import Client
from django.test import override_settings
from django.utils import timezone

from apps.products.models import Product, ProductImage
from apps.core.models import BackgroundJobDispatch
from apps.image_search.models import ImageSearchCache, ImageSearchTask
from apps.tenants.services import TenantService
from apps.tenants.tests.auth import create_operator_key, owner_client


def test_image_search_task_has_dedicated_queue_and_result_backend():
    from apps.image_search.tasks import search_images_for_product

    assert search_images_for_product.queue == 'image_search'
    assert search_images_for_product.ignore_result is False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def local_expensive_start_cache(monkeypatch, request):
    """Keep throttle/budget tests deterministic without a Redis dependency."""
    from apps.core import throttling

    cache = LocMemCache(f'image-start-{request.node.nodeid}', {})
    monkeypatch.setattr(throttling, 'coordination_cache', cache)
    monkeypatch.setattr(throttling.PrincipalScopedRateThrottle, 'cache', cache)
    monkeypatch.setattr(throttling.TenantScopedRateThrottle, 'cache', cache)
    return cache


@pytest.fixture()
def tenant_client(db):
    """Тенант + аутентифицированный Client."""
    tenant, _ = TenantService.create_tenant(
        'Test Corp', 'test-corp-img', 'img@test.com', 'pass12345',
    )
    plaintext = create_operator_key(tenant)
    client = Client()
    client.defaults['HTTP_AUTHORIZATION'] = f'Bearer {plaintext}'
    return client


@pytest.fixture()
def product(tenant_client, db):
    """Товар тенанта из tenant_client."""
    from apps.tenants.models import Tenant
    tenant = Tenant.objects.get(slug='test-corp-img')
    return Product.objects.create(
        tenant=tenant,
        article='TEST123',
        name='Тестовая запчасть',
        brand='BOSCH',
        price='500.00',
    )


@pytest.fixture()
def product_image(product, db):
    """ProductImage со статусом needs_review."""
    return ProductImage.objects.create(
        product=product,
        s3_key='products/test/1/abc123.jpg',
        s3_key_thumb='products/test/1/abc123_thumb.jpg',
        sha256='abc123',
        position=0,
        status=ProductImage.Status.NEEDS_REVIEW,
    )


@pytest.mark.django_db
def test_manual_upload_rejects_file_before_unbounded_read(tenant_client, product, settings):
    settings.MAX_IMAGE_UPLOAD_BYTES = 10
    upload = SimpleUploadedFile('oversized.jpg', b'x' * 11, content_type='image/jpeg')

    response = tenant_client.post(
        f'/api/v1/products/{product.pk}/images/upload/',
        {'image': upload},
    )

    assert response.status_code == 400
    assert response.json()['code'] == 'validation_error'
    assert 'превышает' in response.json()['errors']['image'][0]


@pytest.mark.django_db
def test_manual_upload_persists_actual_storage_names(product):
    from PIL import Image

    from apps.image_search.services.moderation import upload_image

    payload = io.BytesIO()
    Image.new('RGB', (64, 64), 'white').save(payload, format='JPEG')
    storage = MagicMock()
    storage.save.side_effect = [
        'dev/products/manual/original_suffixed.jpg',
        'dev/products/manual/thumb_suffixed.jpg',
    ]

    with patch('apps.image_search.services.moderation.default_storage', storage):
        image = upload_image(product, payload.getvalue())

    assert image.s3_key == 'dev/products/manual/original_suffixed.jpg'
    assert image.s3_key_thumb == 'dev/products/manual/thumb_suffixed.jpg'
    assert (image.resolution_w, image.resolution_h) == (64, 64)
    assert image.file_size_kb is not None


@pytest.mark.django_db
def test_manual_upload_compensates_partial_storage_failure(product):
    from PIL import Image

    from apps.image_search.services.moderation import upload_image

    payload = io.BytesIO()
    Image.new('RGB', (64, 64), 'white').save(payload, format='JPEG')
    storage = MagicMock()
    storage.save.side_effect = [
        'dev/products/manual/original.jpg',
        OSError('thumb failed'),
    ]

    with patch('apps.image_search.services.moderation.default_storage', storage):
        image = upload_image(product, payload.getvalue())

    assert image is None
    storage.delete.assert_called_once_with('dev/products/manual/original.jpg')
    assert not product.images.exists()


# ---------------------------------------------------------------------------
# GET /api/v1/products/{pk}/images/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestImageListView:
    """Тесты списка изображений товара."""

    def test_пустой_список_при_отсутствии_фото(self, tenant_client, product):
        resp = tenant_client.get(f'/api/v1/products/{product.pk}/images/')
        assert resp.status_code == 200
        data = resp.json()
        assert data['status'] == 'ok'
        assert data['data'] == []

    def test_возвращает_изображения_товара(self, tenant_client, product, product_image):
        resp = tenant_client.get(f'/api/v1/products/{product.pk}/images/')
        assert resp.status_code == 200
        assert len(resp.json()['data']) == 1
        assert resp.json()['data'][0]['id'] == product_image.pk

    def test_чужой_товар_404(self, db):
        """Запрос к товару другого тенанта возвращает 404."""
        TenantService.create_tenant('Other', 'other-img', 'o@o.com', 'pass12345')

        from apps.tenants.models import Tenant
        t1 = Tenant.objects.get(slug='other-img')
        p = Product.objects.create(
            tenant=t1, article='X', name='X', price='1.00',
        )
        # Создаём ещё одного тенанта и пытаемся получить товар первого
        tenant3, _ = TenantService.create_tenant(
            'Third', 'third-img', 't@t.com', 'pass12345',
        )
        key3 = create_operator_key(tenant3)
        client3 = Client(HTTP_AUTHORIZATION=f'Bearer {key3}')
        resp = client3.get(f'/api/v1/products/{p.pk}/images/')
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/products/{pk}/images/search/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestImageSearchView:
    """Тесты запуска поиска изображений."""

    def test_запуск_возвращает_task_id(
        self, tenant_client, product, django_capture_on_commit_callbacks,
    ):
        with (
            patch('apps.core.tasks.execute_background_dispatch.apply_async') as publish,
            django_capture_on_commit_callbacks(execute=True),
        ):
            resp = tenant_client.post(
                f'/api/v1/products/{product.pk}/images/search/',
                data={'idempotency_key': str(uuid.uuid4())},
                content_type='application/json',
            )
        assert resp.status_code == 200
        tracking = ImageSearchTask.objects.get(
            tenant=product.tenant,
            product=product,
            task_id=resp.json()['data']['task_id'],
        )
        assert tracking.dispatch is not None
        assert tracking.dispatch.task_name == (
            'apps.image_search.tasks.search_images_for_product'
        )
        assert tracking.dispatch.args == [product.pk, tracking.pk]
        publish.assert_called_once()

    def test_обычный_запуск_не_инвалидирует_свежий_cache(
        self, tenant_client, product,
    ):
        from apps.image_search.services.pipeline import build_cache_key

        cached = ImageSearchCache.objects.create(
            cache_key=build_cache_key(product),
            results={'saved_count': 0, 'cached': True},
            expires_at=timezone.now() + timedelta(days=1),
        )
        response = tenant_client.post(
            f'/api/v1/products/{product.pk}/images/search/',
            data={'idempotency_key': str(uuid.uuid4())},
            content_type='application/json',
        )

        assert response.status_code == 200
        assert ImageSearchCache.objects.filter(pk=cached.pk).exists()

    def test_retry_returns_original_task_without_spending_budget_again(
        self,
        tenant_client,
        product,
    ):
        idempotency_key = str(uuid.uuid4())
        payload = {'idempotency_key': idempotency_key}

        first = tenant_client.post(
            f'/api/v1/products/{product.pk}/images/search/',
            data=payload,
            content_type='application/json',
        )
        with patch(
            'apps.image_search.views.consume_transactional_tenant_daily_budget',
            side_effect=AssertionError('retry consumed budget'),
        ):
            retry = tenant_client.post(
                f'/api/v1/products/{product.pk}/images/search/',
                data=payload,
                content_type='application/json',
            )

        assert first.status_code == 200
        assert retry.status_code == 200
        assert retry.json()['data'] == first.json()['data']
        assert ImageSearchTask.objects.count() == 1
        assert BackgroundJobDispatch.objects.count() == 1

    def test_same_key_with_different_single_intent_is_conflict(
        self,
        tenant_client,
        product,
    ):
        second_product = Product.objects.create(
            tenant=product.tenant,
            article='SECOND',
            name='Second product',
            price='1.00',
        )
        idempotency_key = str(uuid.uuid4())

        first = tenant_client.post(
            f'/api/v1/products/{product.pk}/images/search/',
            data={'idempotency_key': idempotency_key},
            content_type='application/json',
        )
        conflict = tenant_client.post(
            f'/api/v1/products/{second_product.pk}/images/search/',
            data={'idempotency_key': idempotency_key},
            content_type='application/json',
        )

        assert first.status_code == 200
        assert conflict.status_code == 409
        assert conflict.json()['code'] == 'idempotency_conflict'
        assert ImageSearchTask.objects.count() == 1

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_single_retries_create_one_canonical_task(
        self,
        tenant_client,
        product,
    ):
        authorization = tenant_client.defaults['HTTP_AUTHORIZATION']
        idempotency_key = str(uuid.uuid4())
        barrier = threading.Barrier(2)

        def submit():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = Client(HTTP_AUTHORIZATION=authorization).post(
                    f'/api/v1/products/{product.pk}/images/search/',
                    data={'idempotency_key': idempotency_key},
                    content_type='application/json',
                )
                return response.status_code, response.json()
            finally:
                connections.close_all()

        with patch(
            'apps.core.tasks.execute_background_dispatch.apply_async',
        ), ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit(), range(2)))

        assert [status_code for status_code, _ in results] == [200, 200]
        assert results[0][1]['data'] == results[1][1]['data']
        assert ImageSearchTask.objects.count() == 1
        assert BackgroundJobDispatch.objects.count() == 1

    def test_api_key_cannot_force_cache_invalidation(self, tenant_client, product):
        from apps.image_search.services.pipeline import build_cache_key

        cached = ImageSearchCache.objects.create(
            cache_key=build_cache_key(product),
            results={'saved_count': 0},
            expires_at=timezone.now() + timedelta(days=1),
        )

        response = tenant_client.post(
            f'/api/v1/products/{product.pk}/images/search/',
            data={'force': True, 'idempotency_key': str(uuid.uuid4())},
            content_type='application/json',
        )

        assert response.status_code == 403
        assert ImageSearchCache.objects.filter(pk=cached.pk).exists()
        assert not BackgroundJobDispatch.objects.exists()

    def test_owner_can_force_cache_invalidation(self, product):
        from apps.image_search.services.pipeline import build_cache_key

        cached = ImageSearchCache.objects.create(
            cache_key=build_cache_key(product),
            results={'saved_count': 0},
            expires_at=timezone.now() + timedelta(days=1),
        )
        response = owner_client(product.tenant).post(
            f'/api/v1/products/{product.pk}/images/search/',
            data={'force': True, 'idempotency_key': str(uuid.uuid4())},
            content_type='application/json',
        )

        assert response.status_code == 200
        assert not ImageSearchCache.objects.filter(pk=cached.pk).exists()
        tracking = ImageSearchTask.objects.get(product=product)
        assert BackgroundJobDispatch.objects.filter(
            task_name='apps.image_search.tasks.search_images_for_product',
            args=[product.pk, tracking.pk],
        ).count() == 1

    @override_settings(IMAGE_SEARCH_TENANT_DAILY_JOBS=1)
    def test_cached_starts_still_consume_hard_daily_job_budget(
        self, tenant_client, product,
    ):
        from apps.image_search.services.pipeline import build_cache_key

        ImageSearchCache.objects.create(
            cache_key=build_cache_key(product),
            results={'saved_count': 0, 'cached': True},
            expires_at=timezone.now() + timedelta(days=1),
        )

        first = tenant_client.post(
            f'/api/v1/products/{product.pk}/images/search/',
            data={'idempotency_key': str(uuid.uuid4())},
            content_type='application/json',
        )
        second = tenant_client.post(
            f'/api/v1/products/{product.pk}/images/search/',
            data={'idempotency_key': str(uuid.uuid4())},
            content_type='application/json',
        )

        assert first.status_code == 200
        assert second.status_code == 429
        assert BackgroundJobDispatch.objects.count() == 1

    def test_status_возвращает_диагностику_задачи(self, tenant_client, product):
        task_result = {
            'saved_count': 0,
            'found_count': 30,
            'rejected_count': 30,
            'eligible_count': 0,
            'reason_code': 'rejected_by_relevance',
            'message': 'Сервисы нашли 30 изображений, но все отклонены.',
            'sources': ['brave', 'tavily'],
        }
        ImageSearchTask.objects.create(
            tenant=product.tenant,
            product=product,
            task_id='task-1',
        )
        with patch('apps.image_search.views.AsyncResult') as async_result:
            async_result.return_value.state = 'SUCCESS'
            async_result.return_value.result = task_result
            resp = tenant_client.get(
                f'/api/v1/products/{product.pk}/images/search/task-1/',
            )

        assert resp.status_code == 200
        assert resp.json()['data']['state'] == 'done'
        assert resp.json()['data']['found_count'] == 30
        assert resp.json()['data']['reason_code'] == 'rejected_by_relevance'

    def test_status_uses_persistent_dispatch_after_celery_result_expired(
        self, tenant_client, product,
    ):
        dispatch = BackgroundJobDispatch.objects.create(
            task_name='apps.image_search.tasks.search_images_for_product',
            queue='image_search',
            args=[product.pk],
            status=BackgroundJobDispatch.Status.SUCCEEDED,
            result={'saved_count': 2, 'found_count': 4},
        )
        ImageSearchTask.objects.create(
            tenant=product.tenant,
            product=product,
            task_id='durable-task-1',
            dispatch=dispatch,
        )

        with patch('apps.image_search.views.AsyncResult') as async_result:
            response = tenant_client.get(
                f'/api/v1/products/{product.pk}/images/search/durable-task-1/',
            )

        assert response.status_code == 200
        assert response.json()['data'] == {
            'state': 'done', 'saved_count': 2, 'found_count': 4,
        }
        async_result.assert_not_called()

    def test_domain_success_wins_if_worker_dies_before_dispatch_success_cas(
        self,
        tenant_client,
        product,
    ):
        dispatch = BackgroundJobDispatch.objects.create(
            task_name='apps.image_search.tasks.search_images_for_product',
            queue='image_search',
            args=[product.pk, 999],
            status=BackgroundJobDispatch.Status.FAILED,
            result={
                'reason_code': 'outcome_uncertain',
                'message': 'Stale lease marker',
            },
        )
        tracking = ImageSearchTask.objects.create(
            tenant=product.tenant,
            product=product,
            task_id='domain-success-before-dispatch-cas',
            dispatch=dispatch,
            status=ImageSearchTask.Status.SUCCEEDED,
            result={
                'saved_count': 1,
                'reason_code': 'found',
                'message': 'Сохранено фотографий: 1.',
            },
            finished_at=timezone.now(),
        )
        dispatch.args = [product.pk, tracking.pk]
        dispatch.save(update_fields=['args', 'updated_at'])

        response = tenant_client.get(
            f'/api/v1/products/{product.pk}/images/search/{tracking.task_id}/',
        )

        assert response.status_code == 200
        assert response.json()['data'] == {
            'state': 'done',
            'saved_count': 1,
            'reason_code': 'found',
            'message': 'Сохранено фотографий: 1.',
        }

    def test_status_surfaces_durable_uncertain_provider_outcome(
        self, tenant_client, product,
    ):
        dispatch = BackgroundJobDispatch.objects.create(
            task_name='apps.image_search.tasks.search_images_for_product',
            queue='image_search',
            args=[product.pk],
            status=BackgroundJobDispatch.Status.FAILED,
            result={
                'reason_code': 'outcome_uncertain',
                'message': 'Результат внешнего провайдера неизвестен.',
            },
        )
        ImageSearchTask.objects.create(
            tenant=product.tenant,
            product=product,
            task_id='durable-uncertain-task',
            dispatch=dispatch,
        )

        response = tenant_client.get(
            f'/api/v1/products/{product.pk}/images/search/durable-uncertain-task/',
        )

        assert response.status_code == 200
        assert response.json()['data'] == {
            'state': 'failed',
            'saved_count': 0,
            'reason_code': 'outcome_uncertain',
            'message': 'Результат внешнего провайдера неизвестен.',
        }

    @override_settings(CELERY_TASK_TIME_LIMIT=60)
    def test_lost_legacy_result_does_not_stay_running_forever(
        self, tenant_client, product,
    ):
        tracking = ImageSearchTask.objects.create(
            tenant=product.tenant,
            product=product,
            task_id='lost-legacy-task',
        )
        ImageSearchTask.objects.filter(pk=tracking.pk).update(
            created_at=timezone.now() - timedelta(minutes=7),
        )
        with patch('apps.image_search.views.AsyncResult') as async_result:
            async_result.return_value.state = 'PENDING'
            response = tenant_client.get(
                f'/api/v1/products/{product.pk}/images/search/lost-legacy-task/',
            )

        assert response.status_code == 200
        assert response.json()['data']['state'] == 'failed'
        assert response.json()['data']['reason_code'] == 'task_result_lost'

    def test_status_rejects_task_from_another_tenant(
        self, tenant_client, product,
    ):
        other_tenant, _ = TenantService.create_tenant(
            'Foreign Task', 'foreign-task', 'foreign@task.test', 'pass12345',
        )
        other_product = Product.objects.create(
            tenant=other_tenant,
            article='FOREIGN',
            name='Foreign',
            price='1.00',
        )
        ImageSearchTask.objects.create(
            tenant=other_tenant,
            product=other_product,
            task_id='foreign-task-id',
        )

        with patch('apps.image_search.views.AsyncResult') as async_result:
            response = tenant_client.get(
                f'/api/v1/products/{product.pk}/images/search/foreign-task-id/',
            )

        assert response.status_code == 404
        async_result.assert_not_called()


@pytest.mark.django_db
def test_bulk_search_rejects_more_than_hard_batch_cap(tenant_client, product):
    with patch('apps.image_search.services.dispatch.create_image_search_task') as enqueue:
        response = tenant_client.post(
            '/api/v1/images/bulk-search/',
            data={
                'product_ids': list(range(1, 27)),
                'idempotency_key': str(uuid.uuid4()),
            },
            content_type='application/json',
        )

    assert response.status_code == 400
    assert not ImageSearchTask.objects.exists()
    enqueue.assert_not_called()


@pytest.mark.django_db
def test_bulk_search_retry_is_canonical_and_conflicting_list_is_409(
    tenant_client,
    product,
):
    second = Product.objects.create(
        tenant=product.tenant,
        article='BULK-SECOND',
        name='Bulk second',
        price='1.00',
    )
    idempotency_key = str(uuid.uuid4())
    payload = {
        'product_ids': [second.pk, product.pk, second.pk],
        'idempotency_key': idempotency_key,
    }

    first = tenant_client.post(
        '/api/v1/images/bulk-search/',
        data=payload,
        content_type='application/json',
    )
    retry = tenant_client.post(
        '/api/v1/images/bulk-search/',
        data={
            'product_ids': [product.pk, second.pk],
            'idempotency_key': idempotency_key,
        },
        content_type='application/json',
    )
    conflict = tenant_client.post(
        '/api/v1/images/bulk-search/',
        data={
            'product_ids': [product.pk],
            'idempotency_key': idempotency_key,
        },
        content_type='application/json',
    )

    assert first.status_code == 200
    assert retry.status_code == 200
    assert retry.json()['data'] == first.json()['data']
    assert conflict.status_code == 409
    assert ImageSearchTask.objects.count() == 2
    assert BackgroundJobDispatch.objects.count() == 2


@pytest.mark.django_db
def test_expired_terminal_image_intent_allows_a_new_attempt(
    tenant_client,
    product,
    settings,
):
    from apps.core.retention import purge_retained_data
    from apps.image_search.models import ImageSearchIntent

    settings.IMAGE_SEARCH_TASK_RETENTION_DAYS = 30
    settings.RETENTION_PURGE_BATCH_SIZE = 100
    idempotency_key = str(uuid.uuid4())
    payload = {'idempotency_key': idempotency_key}
    first = tenant_client.post(
        f'/api/v1/products/{product.pk}/images/search/',
        data=payload,
        content_type='application/json',
    )
    old = timezone.now() - timedelta(days=31)
    intent = ImageSearchIntent.objects.get(idempotency_key=idempotency_key)
    task = intent.tasks.get()
    task.dispatch.status = BackgroundJobDispatch.Status.SUCCEEDED
    task.dispatch.finished_at = old
    task.dispatch.save(update_fields=['status', 'finished_at', 'updated_at'])
    ImageSearchIntent.objects.filter(pk=intent.pk).update(created_at=old)
    ImageSearchTask.objects.filter(pk=task.pk).update(created_at=old)

    purge_retained_data()
    second = tenant_client.post(
        f'/api/v1/products/{product.pk}/images/search/',
        data=payload,
        content_type='application/json',
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()['data']['task_id'] != first.json()['data']['task_id']


# ---------------------------------------------------------------------------
# POST approve / reject / set_primary
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestModerationViews:
    """Тесты approve, reject, set_primary."""

    def test_approve_меняет_статус(self, tenant_client, product, product_image):
        resp = owner_client(product.tenant).post(
            f'/api/v1/products/{product.pk}/images/{product_image.pk}/approve/',
        )
        assert resp.status_code == 200
        assert resp.json()['data']['status'] == 'auto_approved'
        product_image.refresh_from_db()
        assert product_image.status == ProductImage.Status.AUTO_APPROVED
        assert product_image.is_primary is True

    def test_approve_не_меняет_существующее_главное(self, tenant_client, product, product_image):
        other = ProductImage.objects.create(
            product=product,
            s3_key='products/test/1/other.jpg',
            sha256='other',
            position=1,
            is_primary=True,
        )

        resp = owner_client(product.tenant).post(
            f'/api/v1/products/{product.pk}/images/{product_image.pk}/approve/',
        )

        assert resp.status_code == 200
        product_image.refresh_from_db()
        other.refresh_from_db()
        assert product_image.is_primary is False
        assert other.is_primary is True

    def test_reject_меняет_статус(self, tenant_client, product, product_image):
        resp = owner_client(product.tenant).post(
            f'/api/v1/products/{product.pk}/images/{product_image.pk}/reject/',
        )
        assert resp.status_code == 200
        assert resp.json()['data']['status'] == 'rejected'
        product_image.refresh_from_db()
        assert product_image.status == ProductImage.Status.REJECTED

    def test_reject_снимает_главное_фото(self, tenant_client, product, product_image):
        product_image.is_primary = True
        product_image.save(update_fields=['is_primary'])

        resp = owner_client(product.tenant).post(
            f'/api/v1/products/{product.pk}/images/{product_image.pk}/reject/',
        )

        assert resp.status_code == 200
        product_image.refresh_from_db()
        assert product_image.status == ProductImage.Status.REJECTED
        assert product_image.is_primary is False

    def test_set_primary_устанавливает_флаг(self, tenant_client, product, product_image):
        resp = tenant_client.put(
            f'/api/v1/products/{product.pk}/images/{product_image.pk}/set_primary/',
        )
        assert resp.status_code == 200
        assert resp.json()['data']['is_primary'] is True
        product_image.refresh_from_db()
        assert product_image.is_primary is True

    def test_set_primary_снимает_флаг_с_других(self, tenant_client, product, product_image):
        """При смене главного фото флаг снимается с предыдущего главного."""
        other = ProductImage.objects.create(
            product=product,
            s3_key='products/test/1/other.jpg',
            sha256='other',
            position=1,
            is_primary=True,
        )
        tenant_client.put(
            f'/api/v1/products/{product.pk}/images/{product_image.pk}/set_primary/',
        )
        other.refresh_from_db()
        assert other.is_primary is False


# ---------------------------------------------------------------------------
# DELETE /api/v1/products/{pk}/images/{image_pk}/
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestImageDeleteView:
    """Тесты удаления изображения."""

    def test_delete_удаляет_объект(self, tenant_client, product, product_image):
        resp = tenant_client.delete(
            f'/api/v1/products/{product.pk}/images/{product_image.pk}/',
        )
        assert resp.status_code == 204
        # soft-delete: запись остаётся в БД со статусом rejected (для дедупликации)
        image = ProductImage.objects.get(pk=product_image.pk)
        assert image.status == ProductImage.Status.REJECTED

    def test_product_soft_delete_сохраняет_медиа_до_retention_purge(
        self,
        product,
        django_capture_on_commit_callbacks,
    ):
        image = ProductImage.objects.create(
            product=product,
            s3_key='dev/products/test-corp-img/auto_parts/brakes/original.jpg',
            s3_key_thumb='dev/products/test-corp-img/auto_parts/brakes/thumb.jpg',
            s3_key_preview='dev/products/test-corp-img/auto_parts/brakes/preview.jpg',
            sha256='delete-media',
            position=0,
        )

        with (
            patch('apps.core.storage.default_storage.delete') as storage_delete,
            django_capture_on_commit_callbacks(execute=True),
        ):
            product.delete()

            assert ProductImage.objects.filter(pk=image.pk).exists()
            storage_delete.assert_not_called()

            Product.all_objects.get(pk=product.pk).hard_delete()

        assert not ProductImage.objects.filter(pk=image.pk).exists()
        deleted_keys = {call.args[0] for call in storage_delete.call_args_list}
        assert deleted_keys == {
            'dev/products/test-corp-img/auto_parts/brakes/original.jpg',
            'dev/products/test-corp-img/auto_parts/brakes/thumb.jpg',
            'dev/products/test-corp-img/auto_parts/brakes/preview.jpg',
        }

    def test_delete_чужого_изображения_404(self, db):
        """Нельзя удалить изображение чужого тенанта."""
        TenantService.create_tenant('A', 'a-del', 'a@a.com', 'pass12345')
        tenant2, _ = TenantService.create_tenant('B', 'b-del', 'b@b.com', 'pass12345')
        k2 = create_operator_key(tenant2)
        from apps.tenants.models import Tenant
        t1 = Tenant.objects.get(slug='a-del')
        p = Product.objects.create(tenant=t1, article='A', name='A', price='1.00')
        img = ProductImage.objects.create(
            product=p, s3_key='k', sha256='s', position=0,
        )
        client2 = Client(HTTP_AUTHORIZATION=f'Bearer {k2}')
        resp = client2.delete(f'/api/v1/products/{p.pk}/images/{img.pk}/')
        assert resp.status_code == 404

    def test_storage_delete_is_cancelled_when_db_transaction_rolls_back(
        self,
        product_image,
        django_capture_on_commit_callbacks,
    ):
        from django.db import transaction

        image_pk = product_image.pk
        with (
            patch('apps.core.storage.default_storage.delete') as storage_delete,
            django_capture_on_commit_callbacks(execute=True),
        ):
            with pytest.raises(RuntimeError, match='rollback'):
                with transaction.atomic():
                    product_image.delete()
                    raise RuntimeError('rollback')

        assert ProductImage.objects.filter(pk=image_pk).exists()
        storage_delete.assert_not_called()
