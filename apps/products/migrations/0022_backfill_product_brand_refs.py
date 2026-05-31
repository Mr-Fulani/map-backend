from django.db import migrations

from apps.products.enrichment import normalize_part_code


def resolve_brand(ProductBrand, ProductBrandAlias, raw_brand: str, source_id: str):
    raw_brand = (raw_brand or '').strip()
    normalized = normalize_part_code(raw_brand)
    if not normalized:
        return None

    alias = ProductBrandAlias.objects.select_related('brand').filter(
        normalized_alias=normalized,
        brand__is_active=True,
    ).first()
    if alias is not None:
        return alias.brand

    brand, _ = ProductBrand.objects.get_or_create(
        normalized_name=normalized,
        defaults={
            'name': raw_brand[:150],
            'source_id': source_id,
            'confidence': 0.8,
            'needs_review': False,
            'is_active': True,
        },
    )
    ProductBrandAlias.objects.get_or_create(
        normalized_alias=normalized,
        defaults={
            'brand': brand,
            'alias': raw_brand[:150],
            'source_id': source_id,
            'confidence': 0.8,
            'needs_review': False,
        },
    )
    return brand


def backfill_product_brand_refs(apps, schema_editor):
    Product = apps.get_model('products', 'Product')
    GlobalPart = apps.get_model('products', 'GlobalPart')
    ProductBrand = apps.get_model('products', 'ProductBrand')
    ProductBrandAlias = apps.get_model('products', 'ProductBrandAlias')

    for product in Product.objects.filter(brand_ref__isnull=True).exclude(brand='').iterator():
        brand = resolve_brand(ProductBrand, ProductBrandAlias, product.brand, 'product_backfill')
        if brand is not None:
            product.brand_ref_id = brand.pk
            product.save(update_fields=['brand_ref'])

    for part in GlobalPart.objects.filter(brand_ref__isnull=True).exclude(brand='').iterator():
        brand = resolve_brand(ProductBrand, ProductBrandAlias, part.brand, 'global_part_backfill')
        if brand is not None:
            part.brand_ref_id = brand.pk
            part.save(update_fields=['brand_ref'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0021_seed_base_product_brands'),
    ]

    operations = [
        migrations.RunPython(backfill_product_brand_refs, noop),
    ]
