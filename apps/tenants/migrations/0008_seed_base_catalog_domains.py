from django.db import migrations

from apps.products.catalog_category_seed import BASE_CATALOG_DOMAINS


NEW_DOMAIN_SLUGS = [
    'transport',
    'electronics',
    'home_garden',
    'beauty_health',
    'sports_recreation',
    'kids',
    'pets',
    'construction_repair',
    'business_equipment',
    'real_estate',
    'services',
]


def seed_base_catalog_domains(apps, schema_editor):
    CatalogDomain = apps.get_model('tenants', 'CatalogDomain')
    for data in BASE_CATALOG_DOMAINS:
        CatalogDomain.objects.update_or_create(slug=data['slug'], defaults=data)


def unseed_base_catalog_domains(apps, schema_editor):
    CatalogDomain = apps.get_model('tenants', 'CatalogDomain')
    CatalogDomain.objects.filter(slug__in=NEW_DOMAIN_SLUGS).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0007_tenantcatalogdomain_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_base_catalog_domains, unseed_base_catalog_domains),
    ]
