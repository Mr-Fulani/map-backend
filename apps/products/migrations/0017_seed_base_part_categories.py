from django.db import migrations

from apps.products.part_category_seed import (
    BASE_PART_CATEGORY_TREE, normalize_category_name,
)


SEED_SOURCE = 'platform_auto_parts_seed'


def seed_base_part_categories(apps, schema_editor):
    PartCategory = apps.get_model('products', 'PartCategory')
    Tenant = apps.get_model('tenants', 'Tenant')
    CatalogDomain = apps.get_model('tenants', 'CatalogDomain')
    TenantCatalogDomain = apps.get_model('tenants', 'TenantCatalogDomain')
    TenantCatalogCategory = apps.get_model('products', 'TenantCatalogCategory')
    auto_parts_domain = CatalogDomain.objects.filter(slug='auto_parts').first()

    for domain in CatalogDomain.objects.all():
        TenantCatalogCategory.objects.filter(
            root_domain__isnull=True,
            domain=domain.slug,
        ).update(root_domain=domain)

    for root in BASE_PART_CATEGORY_TREE:
        root_category, _ = PartCategory.objects.update_or_create(
            normalized_name=normalize_category_name(root['name']),
            defaults={
                'name': root['name'],
                'parent': None,
                'aliases': root.get('aliases', []),
                'fitment_required': root.get('fitment_required', True),
            },
        )
        for child_name, aliases, fitment_required in root.get('children', []):
            PartCategory.objects.update_or_create(
                normalized_name=normalize_category_name(child_name),
                defaults={
                    'name': child_name,
                    'parent': root_category,
                    'aliases': aliases,
                    'fitment_required': fitment_required,
                },
            )

    for tenant in Tenant.objects.exclude(catalog_domain=''):
        domain = CatalogDomain.objects.filter(slug=tenant.catalog_domain).first()
        if domain is not None:
            TenantCatalogDomain.objects.update_or_create(
                tenant=tenant,
                domain=domain,
                defaults={'is_enabled': True},
            )

    tenants = Tenant.objects.filter(catalog_domain__in=['auto_parts', 'mixed'])
    for tenant in tenants:
        if auto_parts_domain is None:
            continue
        TenantCatalogDomain.objects.update_or_create(
            tenant=tenant,
            domain=auto_parts_domain,
            defaults={'is_enabled': True},
        )
        for root in BASE_PART_CATEGORY_TREE:
            root_normalized = normalize_category_name(root['name'])
            tenant_root, _ = TenantCatalogCategory.objects.get_or_create(
                tenant=tenant,
                parent__isnull=True,
                normalized_name=root_normalized,
                defaults={
                    'name': root['name'],
                    'root_domain': auto_parts_domain,
                    'domain': 'auto_parts',
                    'aliases': root.get('aliases', []),
                    'external_source': SEED_SOURCE,
                    'external_id': f'root:{root_normalized}',
                    'is_active': True,
                },
            )
            for child_name, aliases, _fitment_required in root.get('children', []):
                child_normalized = normalize_category_name(child_name)
                TenantCatalogCategory.objects.get_or_create(
                    tenant=tenant,
                    parent=tenant_root,
                        normalized_name=child_normalized,
                        defaults={
                            'name': child_name,
                            'root_domain': auto_parts_domain,
                            'domain': 'auto_parts',
                            'aliases': aliases,
                        'external_source': SEED_SOURCE,
                        'external_id': f'{root_normalized}:{child_normalized}',
                        'is_active': True,
                    },
                )


def unseed_base_part_categories(apps, schema_editor):
    PartCategory = apps.get_model('products', 'PartCategory')
    TenantCatalogCategory = apps.get_model('products', 'TenantCatalogCategory')

    seed_names = []
    for root in BASE_PART_CATEGORY_TREE:
        seed_names.append(root['name'])
        seed_names.extend(child_name for child_name, _aliases, _fitment_required in root.get('children', []))

    PartCategory.objects.filter(
        normalized_name__in=[normalize_category_name(name) for name in seed_names]
    ).delete()
    TenantCatalogCategory.objects.filter(external_source=SEED_SOURCE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0007_tenantcatalogdomain_and_more'),
        ('products', '0016_tenant_catalog_root_domain'),
    ]

    operations = [
        migrations.RunPython(seed_base_part_categories, unseed_base_part_categories),
    ]
