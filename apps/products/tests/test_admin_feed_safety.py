import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import RequestFactory

from apps.products.admin import ProductAdmin, TenantCatalogCategoryAdmin
from apps.products.models import Product, TenantCatalogCategory


@pytest.mark.django_db
def test_product_and_catalog_admin_close_raw_feed_writers():
    request = RequestFactory().get('/admin/products/')
    request.user = get_user_model().objects.create_superuser(
        'product-feed-admin-safety@example.com',
        'pass12345',
    )

    product_admin = ProductAdmin(Product, AdminSite())
    assert {
        'tenant',
        'datasource',
        'article',
        'name',
        'brand',
        'category_1c',
        'catalog_category',
        'condition',
        'description_1c',
        'oem_numbers',
        'stock_qty',
        'price',
        'sync_excluded',
        'deleted_at',
    } <= set(product_admin.get_readonly_fields(request))
    assert product_admin.has_delete_permission(request) is False
    assert 'delete_selected' not in product_admin.get_actions(request)

    category_admin = TenantCatalogCategoryAdmin(
        TenantCatalogCategory,
        AdminSite(),
    )
    assert {
        'tenant',
        'name',
        'normalized_name',
        'parent',
        'external_id',
        'default_image_s3_key',
    } <= set(category_admin.get_readonly_fields(request))
    assert category_admin.has_delete_permission(request) is False
    assert 'delete_selected' not in category_admin.get_actions(request)
