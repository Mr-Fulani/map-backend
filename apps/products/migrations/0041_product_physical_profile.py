import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0040_productparsejob_fallback_origin_key'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductPhysicalProfile',
            fields=[
                (
                    'id',
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                (
                    'source_barcode',
                    models.CharField(blank=True, max_length=64, verbose_name='Штрихкод из 1С'),
                ),
                (
                    'source_length_mm',
                    models.DecimalField(
                        blank=True, decimal_places=3, max_digits=12, null=True,
                        verbose_name='Длина из 1С, мм',
                    ),
                ),
                (
                    'source_width_mm',
                    models.DecimalField(
                        blank=True, decimal_places=3, max_digits=12, null=True,
                        verbose_name='Ширина из 1С, мм',
                    ),
                ),
                (
                    'source_height_mm',
                    models.DecimalField(
                        blank=True, decimal_places=3, max_digits=12, null=True,
                        verbose_name='Высота из 1С, мм',
                    ),
                ),
                (
                    'source_weight_g',
                    models.DecimalField(
                        blank=True, decimal_places=3, max_digits=12, null=True,
                        verbose_name='Вес из 1С, г',
                    ),
                ),
                (
                    'source_vat_rate',
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=5, null=True,
                        verbose_name='НДС из 1С, %',
                    ),
                ),
                (
                    'source_errors',
                    models.JSONField(
                        blank=True, default=dict,
                        verbose_name='Ошибки физических данных из 1С',
                    ),
                ),
                (
                    'source_updated_at',
                    models.DateTimeField(
                        blank=True, null=True,
                        verbose_name='Дата получения физических данных из 1С',
                    ),
                ),
                (
                    'map_barcode',
                    models.CharField(blank=True, max_length=64, verbose_name='Штрихкод MAP'),
                ),
                (
                    'map_length_mm',
                    models.DecimalField(
                        blank=True, decimal_places=3, max_digits=12, null=True,
                        verbose_name='Длина MAP, мм',
                    ),
                ),
                (
                    'map_width_mm',
                    models.DecimalField(
                        blank=True, decimal_places=3, max_digits=12, null=True,
                        verbose_name='Ширина MAP, мм',
                    ),
                ),
                (
                    'map_height_mm',
                    models.DecimalField(
                        blank=True, decimal_places=3, max_digits=12, null=True,
                        verbose_name='Высота MAP, мм',
                    ),
                ),
                (
                    'map_weight_g',
                    models.DecimalField(
                        blank=True, decimal_places=3, max_digits=12, null=True,
                        verbose_name='Вес MAP, г',
                    ),
                ),
                (
                    'map_vat_rate',
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=5, null=True,
                        verbose_name='НДС MAP, %',
                    ),
                ),
                (
                    'product',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='physical_profile',
                        to='products.product',
                        verbose_name='Товар',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='product_physical_profiles',
                        to='tenants.tenant',
                        verbose_name='Тенант',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Физические данные товара',
                'verbose_name_plural': 'Физические данные товаров',
            },
        ),
        migrations.AddIndex(
            model_name='productphysicalprofile',
            index=models.Index(
                fields=['tenant', '-updated_at'],
                name='prd_phys_tenant_updated_idx',
            ),
        ),
    ]
