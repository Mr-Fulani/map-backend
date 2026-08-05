from django.db import migrations


WEB_SEARCH_SOURCE_IDS = ('brave', 'tavily', 'duckduckgo')


def restore_web_search_images_for_review(apps, schema_editor):
    ProductImage = apps.get_model('products', 'ProductImage')
    ProductImage.objects.filter(
        source_id__in=WEB_SEARCH_SOURCE_IDS,
        status='imported',
    ).update(status='needs_review')


class Migration(migrations.Migration):
    dependencies = [
        ('products', '0030_alter_productenrichmentfact_fact_type'),
    ]

    operations = [
        migrations.RunPython(
            restore_web_search_images_for_review,
            migrations.RunPython.noop,
        ),
    ]
