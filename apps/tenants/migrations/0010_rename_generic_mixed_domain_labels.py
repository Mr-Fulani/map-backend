from django.db import migrations


def rename_domain_labels(apps, schema_editor):
    """Переименовывает отображаемые названия доменов generic и mixed."""
    CatalogDomain = apps.get_model('tenants', 'CatalogDomain')
    CatalogDomain.objects.filter(slug='generic').update(
        name='Смешанный каталог',
        short_name='Смешанный',
        seo_title='Смешанный каталог',
        seo_description='Универсальный каталог товаров.',
        seo_h1='Смешанный каталог',
    )
    CatalogDomain.objects.filter(slug='mixed').update(
        name='Авто-ти + Другие товары',
        short_name='Авто + Другие',
        description='Каталог с автозапчастями и товарами других категорий.',
        seo_title='Авто-ти + Другие товары',
        seo_description='Каталог автозапчастей и товаров других направлений.',
        seo_h1='Авто-ти + Другие товары',
    )


def reverse_rename(apps, schema_editor):
    CatalogDomain = apps.get_model('tenants', 'CatalogDomain')
    CatalogDomain.objects.filter(slug='generic').update(
        name='Обычный каталог',
        short_name='Общий',
        seo_title='Обычный каталог',
        seo_description='Универсальный каталог товаров.',
        seo_h1='Обычный каталог',
    )
    CatalogDomain.objects.filter(slug='mixed').update(
        name='Смешанный каталог',
        short_name='Смешанный',
        description='Системный тип tenant-а с товарами разных направлений.',
        seo_title='Смешанный каталог',
        seo_description='Смешанный каталог товаров с безопасной классификацией по типам.',
        seo_h1='Смешанный каталог',
    )


class Migration(migrations.Migration):
    """Переименование generic → Смешанный каталог, mixed → Авто-ти + Другие товары."""

    dependencies = [
        ('tenants', '0009_change_default_catalog_domain_to_unknown'),
    ]

    operations = [
        migrations.RunPython(rename_domain_labels, reverse_code=reverse_rename),
    ]
