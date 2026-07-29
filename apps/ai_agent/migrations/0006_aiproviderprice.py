import decimal

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai_agent', '0005_seed_budget_openai_models'),
    ]

    operations = [
        migrations.CreateModel(
            name='AIProviderPrice',
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
                (
                    'currency',
                    models.CharField(
                        default='USD',
                        max_length=3,
                        validators=[
                            django.core.validators.RegexValidator(
                                message=(
                                    'Валюта должна быть трёхбуквенным '
                                    'кодом ISO 4217.'
                                ),
                                regex='^[A-Z]{3}$',
                            ),
                        ],
                    ),
                ),
                (
                    'input_per_million',
                    models.DecimalField(
                        decimal_places=8,
                        default=decimal.Decimal('0'),
                        max_digits=20,
                    ),
                ),
                (
                    'cached_read_per_million',
                    models.DecimalField(
                        decimal_places=8,
                        default=decimal.Decimal('0'),
                        max_digits=20,
                    ),
                ),
                (
                    'cached_write_per_million',
                    models.DecimalField(
                        decimal_places=8,
                        default=decimal.Decimal('0'),
                        max_digits=20,
                    ),
                ),
                (
                    'output_per_million',
                    models.DecimalField(
                        decimal_places=8,
                        default=decimal.Decimal('0'),
                        max_digits=20,
                    ),
                ),
                ('effective_from', models.DateTimeField(db_index=True)),
                ('source_url', models.URLField(blank=True)),
                ('notes', models.CharField(blank=True, max_length=500)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                (
                    'model',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='provider_prices',
                        to='ai_agent.aimodel',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Цена AI-провайдера',
                'verbose_name_plural': 'Цены AI-провайдеров',
                'ordering': ['-effective_from', '-pk'],
            },
        ),
        migrations.AddConstraint(
            model_name='aiproviderprice',
            constraint=models.UniqueConstraint(
                fields=('model', 'effective_from'),
                name='unique_ai_model_price_effective_from',
            ),
        ),
        migrations.AddConstraint(
            model_name='aiproviderprice',
            constraint=models.CheckConstraint(
                check=models.Q(('input_per_million__gte', 0)),
                name='ai_price_input_nonnegative',
            ),
        ),
        migrations.AddConstraint(
            model_name='aiproviderprice',
            constraint=models.CheckConstraint(
                check=models.Q(('cached_read_per_million__gte', 0)),
                name='ai_price_cached_read_nonnegative',
            ),
        ),
        migrations.AddConstraint(
            model_name='aiproviderprice',
            constraint=models.CheckConstraint(
                check=models.Q(('cached_write_per_million__gte', 0)),
                name='ai_price_cached_write_nonnegative',
            ),
        ),
        migrations.AddConstraint(
            model_name='aiproviderprice',
            constraint=models.CheckConstraint(
                check=models.Q(('output_per_million__gte', 0)),
                name='ai_price_output_nonnegative',
            ),
        ),
        migrations.AddIndex(
            model_name='aiproviderprice',
            index=models.Index(
                fields=['model', '-effective_from'],
                name='ai_price_model_effective_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='aiproviderprice',
            index=models.Index(
                fields=['currency'],
                name='ai_price_currency_idx',
            ),
        ),
    ]
