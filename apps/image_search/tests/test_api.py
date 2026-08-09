"""Тесты API управления изображениями товаров."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.cache.backends.locmem import LocMemCache
from django.test import Client
from django.test import override_settings
from django.utils import timezone

from apps.products.models import Product, ProductImage
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

    def test_запуск_возвращает_task_id(self, tenant_client, product):
        from unittest.mock import patch, MagicMock
        mock_task = MagicMock()
        mock_task.id = 'test-task-uuid'
        with patch('apps.image_search.tasks.search_images_for_product.delay', return_value=mock_task):
            resp = tenant_client.post(f'/api/v1/products/{product.pk}/images/search/')
        assert resp.status_code == 200
        assert resp.json()['data']['task_id'] == 'test-task-uuid'
        assert ImageSearchTask.objects.filter(
            tenant=product.tenant,
            product=product,
            task_id='test-task-uuid',
        ).exists()

    def test_обычный_запуск_не_инвалидирует_свежий_cache(
        self, tenant_client, product,
    ):
        from apps.image_search.services.pipeline import build_cache_key

        cached = ImageSearchCache.objects.create(
            cache_key=build_cache_key(product),
            results={'saved_count': 0, 'cached': True},
            expires_at=timezone.now() + timedelta(days=1),
        )
        task = SimpleNamespace(id='cached-search-task')

        with patch(
            'apps.image_search.tasks.search_images_for_product.delay',
            return_value=task,
        ):
            response = tenant_client.post(
                f'/api/v1/products/{product.pk}/images/search/',
                data={},
                content_type='application/json',
            )

        assert response.status_code == 200
        assert ImageSearchCache.objects.filter(pk=cached.pk).exists()

    def test_api_key_cannot_force_cache_invalidation(self, tenant_client, product):
        from apps.image_search.services.pipeline import build_cache_key

        cached = ImageSearchCache.objects.create(
            cache_key=build_cache_key(product),
            results={'saved_count': 0},
            expires_at=timezone.now() + timedelta(days=1),
        )

        with patch('apps.image_search.tasks.search_images_for_product.delay') as delay:
            response = tenant_client.post(
                f'/api/v1/products/{product.pk}/images/search/',
                data={'force': True},
                content_type='application/json',
            )

        assert response.status_code == 403
        assert ImageSearchCache.objects.filter(pk=cached.pk).exists()
        delay.assert_not_called()

    def test_owner_can_force_cache_invalidation(self, product):
        from apps.image_search.services.pipeline import build_cache_key

        cached = ImageSearchCache.objects.create(
            cache_key=build_cache_key(product),
            results={'saved_count': 0},
            expires_at=timezone.now() + timedelta(days=1),
        )
        task = SimpleNamespace(id='forced-search-task')

        with patch(
            'apps.image_search.tasks.search_images_for_product.delay',
            return_value=task,
        ) as delay:
            response = owner_client(product.tenant).post(
                f'/api/v1/products/{product.pk}/images/search/',
                data={'force': True},
                content_type='application/json',
            )

        assert response.status_code == 200
        assert not ImageSearchCache.objects.filter(pk=cached.pk).exists()
        delay.assert_called_once_with(product.pk)

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

        with patch(
            'apps.image_search.tasks.search_images_for_product.delay',
            return_value=SimpleNamespace(id='daily-image-task'),
        ) as delay:
            first = tenant_client.post(
                f'/api/v1/products/{product.pk}/images/search/',
                data={}, content_type='application/json',
            )
            second = tenant_client.post(
                f'/api/v1/products/{product.pk}/images/search/',
                data={}, content_type='application/json',
            )

        assert first.status_code == 200
        assert second.status_code == 429
        delay.assert_called_once_with(product.pk)

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
    with patch('apps.image_search.tasks.search_images_for_product.delay') as delay:
        response = tenant_client.post(
            '/api/v1/images/bulk-search/',
            data={'product_ids': list(range(1, 27))},
            content_type='application/json',
        )

    assert response.status_code == 400
    assert not ImageSearchTask.objects.exists()
    delay.assert_not_called()


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

    def test_product_soft_delete_сохраняет_медиа_до_retention_purge(self, product):
        image = ProductImage.objects.create(
            product=product,
            s3_key='dev/products/test-corp-img/auto_parts/brakes/original.jpg',
            s3_key_thumb='dev/products/test-corp-img/auto_parts/brakes/thumb.jpg',
            s3_key_preview='dev/products/test-corp-img/auto_parts/brakes/preview.jpg',
            sha256='delete-media',
            position=0,
        )

        with patch('django.core.files.storage.default_storage.delete') as storage_delete:
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
