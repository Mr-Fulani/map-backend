from django.db import migrations

from apps.products.catalog_category_seed import BASE_CATEGORY_TEMPLATE_TREE
from apps.products.part_category_seed import normalize_category_name


def seed_catalog_category_templates(apps, schema_editor):
    TenantCatalogDomain = apps.get_model('tenants', 'TenantCatalogDomain')
    TenantCatalogCategory = apps.get_model('products', 'TenantCatalogCategory')

    enabled_roots = TenantCatalogDomain.objects.filter(
        is_enabled=True,
        domain__slug__in=BASE_CATEGORY_TEMPLATE_TREE.keys(),
    ).select_related('tenant', 'domain')

    for enabled_root in enabled_roots:
        root_domain = enabled_root.domain
        seed_source = f'platform_{root_domain.slug}_seed'
        for root in BASE_CATEGORY_TEMPLATE_TREE[root_domain.slug]:
            root_normalized = normalize_category_name(root['name'])
            tenant_root, _ = TenantCatalogCategory.objects.get_or_create(
                tenant=enabled_root.tenant,
                parent__isnull=True,
                root_domain=root_domain,
                normalized_name=root_normalized,
                defaults={
                    'name': root['name'],
                    'domain': root_domain.slug,
                    'aliases': root.get('aliases', []),
                    'external_source': seed_source,
                    'external_id': f'root:{root_normalized}',
                    'is_active': True,
                },
            )
            for child in root.get('children', []):
                if isinstance(child, tuple):
                    child_name, aliases = child[0], child[1]
                else:
                    child_name, aliases = child, []
                child_normalized = normalize_category_name(child_name)
                TenantCatalogCategory.objects.get_or_create(
                    tenant=enabled_root.tenant,
                    parent=tenant_root,
                    normalized_name=child_normalized,
                    defaults={
                        'name': child_name,
                        'root_domain': root_domain,
                        'domain': root_domain.slug,
                        'aliases': aliases,
                        'external_source': seed_source,
                        'external_id': f'{root_normalized}:{child_normalized}',
                        'is_active': True,
                    },
                )


def unseed_catalog_category_templates(apps, schema_editor):
    TenantCatalogCategory = apps.get_model('products', 'TenantCatalogCategory')
    seed_sources = [
        f'platform_{slug}_seed'
        for slug in BASE_CATEGORY_TEMPLATE_TREE
        if slug != 'auto_parts'
    ]
    TenantCatalogCategory.objects.filter(external_source__in=seed_sources).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0008_seed_base_catalog_domains'),
        ('products', '0018_remove_tenantcatalogcategory_unique_root_tenant_catalog_category_name_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_catalog_category_templates, unseed_catalog_category_templates),
    ]
