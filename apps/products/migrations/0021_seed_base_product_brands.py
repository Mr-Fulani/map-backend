from django.db import migrations

from apps.products.brand_seed import BASE_PRODUCT_BRANDS
from apps.products.enrichment import normalize_part_code


def seed_base_product_brands(apps, schema_editor):
    CatalogDomain = apps.get_model('tenants', 'CatalogDomain')
    ProductBrand = apps.get_model('products', 'ProductBrand')
    ProductBrandAlias = apps.get_model('products', 'ProductBrandAlias')

    for data in BASE_PRODUCT_BRANDS:
        brand, _ = ProductBrand.objects.update_or_create(
            normalized_name=normalize_part_code(data['name']),
            defaults={
                'name': data['name'],
                'source_id': 'platform_brand_seed',
                'confidence': 1.0,
                'needs_review': False,
                'is_active': True,
            },
        )
        domains = CatalogDomain.objects.filter(slug__in=data.get('domains', []))
        brand.domains.set(domains)
        aliases = [data['name'], *data.get('aliases', [])]
        for alias in aliases:
            ProductBrandAlias.objects.update_or_create(
                normalized_alias=normalize_part_code(alias),
                defaults={
                    'brand': brand,
                    'alias': alias,
                    'source_id': 'platform_brand_seed',
                    'confidence': 1.0,
                    'needs_review': False,
                },
            )


def unseed_base_product_brands(apps, schema_editor):
    ProductBrand = apps.get_model('products', 'ProductBrand')
    ProductBrand.objects.filter(source_id='platform_brand_seed').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0020_productbrandalias_productbrand_globalpart_brand_ref_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_base_product_brands, unseed_base_product_brands),
    ]
