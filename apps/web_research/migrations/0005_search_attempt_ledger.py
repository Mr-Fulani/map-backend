import django.db.models.deletion

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0040_productparsejob_fallback_origin_key'),
        ('tenants', '0016_webhook_delivery_claim_constraint'),
        ('web_research', '0004_provider_outcome_evidence'),
    ]

    operations = [
        migrations.CreateModel(
            name='WebSearchUsageGate',
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
                ('provider_id', models.SlugField(max_length=50, unique=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='WebSearchWorkflow',
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
                ('operation', models.SlugField(max_length=50, verbose_name='Операция')),
                (
                    'domain_reference',
                    models.CharField(max_length=160, verbose_name='Стабильная доменная ссылка'),
                ),
                (
                    'workflow_key',
                    models.CharField(max_length=160, verbose_name='Ключ бизнес-выполнения'),
                ),
                (
                    'input_fingerprint',
                    models.CharField(max_length=64, verbose_name='Отпечаток неизменяемого плана'),
                ),
                (
                    'input_snapshot',
                    models.JSONField(
                        default=dict,
                        editable=False,
                        verbose_name='Неизменяемый нормализованный план',
                    ),
                ),
                (
                    'status',
                    models.CharField(
                        choices=[
                            ('in_progress', 'Выполняется'),
                            ('apply_pending', 'Ожидает применения'),
                            ('uncertain', 'Требует сверки'),
                            ('applied', 'Применено'),
                            ('reconciled', 'Сверено вручную'),
                        ],
                        db_index=True,
                        default='in_progress',
                        max_length=20,
                        verbose_name='Статус',
                    ),
                ),
                ('applied_at', models.DateTimeField(blank=True, null=True)),
                ('reconciliation_action', models.CharField(blank=True, max_length=40)),
                ('reconciliation_note', models.TextField(blank=True)),
                ('reconciled_at', models.DateTimeField(blank=True, null=True)),
                (
                    'product',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='web_search_workflows',
                        to='products.product',
                        verbose_name='Товар',
                    ),
                ),
                (
                    'run',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='search_workflows',
                        to='web_research.webresearchrun',
                        verbose_name='Исследование',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='web_search_workflows',
                        to='tenants.tenant',
                        verbose_name='Тенант',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Рабочий процесс платного интернет-поиска',
                'verbose_name_plural': 'Рабочие процессы платного интернет-поиска',
                'ordering': ['created_at', 'id'],
                'indexes': [
                    models.Index(
                        fields=['tenant', 'operation', 'domain_reference'],
                        name='webflow_domain_idx',
                    ),
                    models.Index(
                        fields=['tenant', '-updated_at'],
                        name='webflow_tenant_recent_idx',
                    ),
                    models.Index(
                        fields=['status', 'updated_at'],
                        name='webflow_retention_idx',
                    ),
                ],
            },
        ),
        migrations.AddField(
            model_name='webresearchrun',
            name='origin_key',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=160,
                verbose_name='Ключ происхождения запуска',
            ),
        ),
        migrations.AddConstraint(
            model_name='webresearchrun',
            constraint=models.UniqueConstraint(
                condition=models.Q(('origin_key', ''), _negated=True),
                fields=('tenant', 'origin_key'),
                name='unique_tenant_web_research_origin',
            ),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='call_kind',
            field=models.SlugField(
                default='search', max_length=30, verbose_name='Вид вызова',
            ),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='apply_state',
            field=models.CharField(
                choices=[
                    ('pending', 'Ожидает применения'),
                    ('applied', 'Применено или сверено'),
                ],
                db_index=True,
                default='pending',
                max_length=20,
                verbose_name='Применение результата',
            ),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='call_key',
            field=models.CharField(
                blank=True,
                max_length=160,
                verbose_name='Детерминированный ключ вызова',
            ),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='checkpoint_enc',
            field=models.BinaryField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Зашифрованный нормализованный результат',
            ),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='request_fingerprint',
            field=models.CharField(
                blank=True,
                max_length=64,
                verbose_name='Отпечаток запроса',
            ),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='workflow',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='attempts',
                to='web_research.websearchworkflow',
                verbose_name='Рабочий процесс',
            ),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='domain_reference',
            field=models.CharField(blank=True, max_length=160, verbose_name='Доменная ссылка'),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='operation',
            field=models.SlugField(default='web_research', max_length=50, verbose_name='Операция'),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='reconciliation_action',
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='reconciliation_note',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='reconciliation_state',
            field=models.CharField(
                choices=[
                    ('not_required', 'Не требуется'),
                    ('pending', 'Требует сверки'),
                    ('resolved', 'Сверено'),
                ],
                db_index=True,
                default='not_required',
                max_length=20,
                verbose_name='Состояние сверки',
            ),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='reconciled_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='websearchattempt',
            name='tenant',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='web_search_attempts',
                to='tenants.tenant',
                verbose_name='Тенант',
            ),
        ),
        migrations.AlterField(
            model_name='websearchattempt',
            name='run',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='search_attempts',
                to='web_research.webresearchrun',
                verbose_name='Исследование',
            ),
        ),
    ]
