from django.db import migrations, models


def convert_zero_margins_to_inherited(apps, schema_editor):
    """Считает прежний ноль отсутствием собственной наценки."""
    TenantCatalogCategory = apps.get_model('products', 'TenantCatalogCategory')
    TenantCatalogCategory.objects.filter(default_margin_pct=0).update(default_margin_pct=None)


def restore_inherited_margins_as_zero(apps, schema_editor):
    """Возвращает обязательное числовое значение для отката миграции."""
    TenantCatalogCategory = apps.get_model('products', 'TenantCatalogCategory')
    TenantCatalogCategory.objects.filter(default_margin_pct__isnull=True).update(default_margin_pct=0)


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0027_tenantcatalogcategory_default_margin_pct'),
    ]

    operations = [
        migrations.AlterField(
            model_name='tenantcatalogcategory',
            name='default_margin_pct',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                default=None,
                help_text=(
                    'Пустое значение наследует наценку ближайшей '
                    'родительской категории.'
                ),
                max_digits=5,
                null=True,
                verbose_name='Наценка по умолчанию, %',
            ),
        ),
        migrations.RunPython(
            convert_zero_margins_to_inherited,
            restore_inherited_margins_as_zero,
        ),
    ]
