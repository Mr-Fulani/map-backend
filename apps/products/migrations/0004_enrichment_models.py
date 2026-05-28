# Generated manually for tenant-scoped product enrichment MVP.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0003_add_ai_description_to_product'),
        ('tenants', '0003_alter_apikey_created_at_alter_apikey_is_active_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProductAttribute',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('source_id', models.CharField(default='tachka', max_length=50, verbose_name='Источник')),
                ('name', models.CharField(max_length=150, verbose_name='Название')),
                ('raw_name', models.CharField(blank=True, max_length=150, verbose_name='Исходное название')),
                ('value', models.TextField(verbose_name='Значение')),
                ('value_hash', models.CharField(blank=True, max_length=64, verbose_name='Хэш значения')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attributes', to='products.product', verbose_name='Товар')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='product_attributes', to='tenants.tenant', verbose_name='Тенант')),
            ],
            options={
                'verbose_name': 'Характеристика товара',
                'verbose_name_plural': 'Характеристики товаров',
                'indexes': [
                    models.Index(fields=['tenant', 'product'], name='products_pr_tenant__398880_idx'),
                    models.Index(fields=['tenant', 'name'], name='products_pr_tenant__f1ead1_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('tenant', 'product', 'source_id', 'name', 'value_hash'), name='unique_product_attribute_value'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ProductCrossCode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('source_id', models.CharField(default='tachka', max_length=50, verbose_name='Источник')),
                ('manufacturer', models.CharField(blank=True, max_length=100, verbose_name='Производитель')),
                ('code', models.CharField(max_length=100, verbose_name='Код')),
                ('normalized_code', models.CharField(db_index=True, max_length=100, verbose_name='Нормализованный код')),
                ('code_type', models.CharField(choices=[('OEM', 'OEM'), ('Cross', 'Cross'), ('Trade', 'Trade'), ('Unknown', 'Unknown')], default='Unknown', max_length=20, verbose_name='Тип кода')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='cross_codes', to='products.product', verbose_name='Товар')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='product_cross_codes', to='tenants.tenant', verbose_name='Тенант')),
            ],
            options={
                'verbose_name': 'OEM/Cross-код',
                'verbose_name_plural': 'OEM/Cross-коды',
                'indexes': [
                    models.Index(fields=['tenant', 'normalized_code'], name='products_pr_tenant__baf612_idx'),
                    models.Index(fields=['tenant', 'manufacturer', 'normalized_code'], name='products_pr_tenant__7f8758_idx'),
                    models.Index(fields=['product', 'code_type'], name='products_pr_product_96bd11_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('tenant', 'product', 'source_id', 'manufacturer', 'normalized_code', 'code_type'), name='unique_product_cross_code'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ProductEnrichmentFact',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('source_id', models.CharField(default='tachka', max_length=50, verbose_name='Источник')),
                ('fact_type', models.CharField(choices=[('technical', 'Технический'), ('fitment', 'Применяемость'), ('oem', 'OEM/Cross'), ('description_hint', 'Подсказка для описания'), ('warning', 'Предупреждение')], max_length=30, verbose_name='Тип факта')),
                ('name', models.CharField(max_length=150, verbose_name='Название')),
                ('value', models.TextField(verbose_name='Значение')),
                ('value_hash', models.CharField(blank=True, max_length=64, verbose_name='Хэш значения')),
                ('raw_text', models.TextField(blank=True, verbose_name='Исходный текст')),
                ('confidence', models.FloatField(default=1.0, verbose_name='Уверенность')),
                ('needs_review', models.BooleanField(default=False, verbose_name='Нужна проверка')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='enrichment_facts', to='products.product', verbose_name='Товар')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='product_enrichment_facts', to='tenants.tenant', verbose_name='Тенант')),
            ],
            options={
                'verbose_name': 'Факт обогащения товара',
                'verbose_name_plural': 'Факты обогащения товаров',
                'indexes': [
                    models.Index(fields=['tenant', 'product'], name='products_pr_tenant__9048d3_idx'),
                    models.Index(fields=['tenant', 'fact_type'], name='products_pr_tenant__7484b9_idx'),
                    models.Index(fields=['product', 'needs_review'], name='products_pr_product_6ae856_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('tenant', 'product', 'source_id', 'fact_type', 'name', 'value_hash'), name='unique_product_enrichment_fact'),
                ],
            },
        ),
        migrations.CreateModel(
            name='ProductParseJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('brand', models.CharField(max_length=100, verbose_name='Бренд')),
                ('article', models.CharField(max_length=100, verbose_name='Артикул')),
                ('normalized_article', models.CharField(db_index=True, max_length=100, verbose_name='Нормализованный артикул')),
                ('source_id', models.CharField(default='tachka', max_length=50, verbose_name='Источник')),
                ('source_url', models.URLField(blank=True, verbose_name='URL источника')),
                ('status', models.CharField(choices=[('pending', 'Ожидает'), ('running', 'В работе'), ('success', 'Успешно'), ('failed', 'Ошибка'), ('not_found', 'Не найдено'), ('need_review', 'Нужна проверка')], default='pending', max_length=30, verbose_name='Статус')),
                ('error_message', models.TextField(blank=True, verbose_name='Ошибка')),
                ('raw_html', models.TextField(blank=True, verbose_name='Raw HTML')),
                ('raw_text', models.TextField(blank=True, verbose_name='Raw text')),
                ('parsed_data', models.JSONField(blank=True, null=True, verbose_name='Распарсенные данные')),
                ('duration_ms', models.PositiveIntegerField(blank=True, null=True, verbose_name='Длительность, мс')),
                ('started_at', models.DateTimeField(blank=True, null=True, verbose_name='Начато')),
                ('finished_at', models.DateTimeField(blank=True, null=True, verbose_name='Завершено')),
                ('product', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='parse_jobs', to='products.product', verbose_name='Товар')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='product_parse_jobs', to='tenants.tenant', verbose_name='Тенант')),
            ],
            options={
                'verbose_name': 'Задача парсинга товара',
                'verbose_name_plural': 'Задачи парсинга товаров',
                'indexes': [
                    models.Index(fields=['tenant', '-created_at'], name='products_pr_tenant__83192b_idx'),
                    models.Index(fields=['status', '-created_at'], name='products_pr_status_469230_idx'),
                    models.Index(fields=['source_id', 'normalized_article'], name='products_pr_source__6d72fe_idx'),
                    models.Index(fields=['tenant', 'source_id', 'normalized_article'], name='products_pr_tenant__b3613a_idx'),
                ],
            },
        ),
        migrations.CreateModel(
            name='VehicleFitment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('source_id', models.CharField(default='tachka', max_length=50, verbose_name='Источник')),
                ('make', models.CharField(blank=True, max_length=100, verbose_name='Марка')),
                ('model', models.CharField(max_length=150, verbose_name='Модель')),
                ('generation', models.CharField(blank=True, max_length=100, verbose_name='Поколение')),
                ('date_from', models.CharField(blank=True, max_length=20, verbose_name='Дата с')),
                ('date_to', models.CharField(blank=True, max_length=20, verbose_name='Дата по')),
                ('modification', models.CharField(blank=True, max_length=255, verbose_name='Модификация')),
                ('engine_code', models.CharField(blank=True, max_length=100, verbose_name='Код двигателя/кузова')),
                ('power_hp', models.PositiveIntegerField(blank=True, null=True, verbose_name='Мощность, л.с.')),
                ('raw_text', models.TextField(blank=True, verbose_name='Исходная строка')),
                ('confidence', models.FloatField(default=1.0, verbose_name='Уверенность')),
                ('needs_review', models.BooleanField(default=False, verbose_name='Нужна проверка')),
                ('product', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fitments', to='products.product', verbose_name='Товар')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='vehicle_fitments', to='tenants.tenant', verbose_name='Тенант')),
            ],
            options={
                'verbose_name': 'Применяемость',
                'verbose_name_plural': 'Применяемость',
                'indexes': [
                    models.Index(fields=['tenant', 'make', 'model'], name='products_ve_tenant__da630d_idx'),
                    models.Index(fields=['tenant', 'product'], name='products_ve_tenant__9e8185_idx'),
                    models.Index(fields=['product', 'needs_review'], name='products_ve_product_5d0fe6_idx'),
                ],
                'constraints': [
                    models.UniqueConstraint(fields=('tenant', 'product', 'source_id', 'make', 'model', 'generation', 'modification', 'engine_code', 'power_hp'), name='unique_vehicle_fitment'),
                ],
            },
        ),
    ]
