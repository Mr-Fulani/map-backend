from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('image_search', '0009_provider_outcome_evidence'),
    ]

    operations = [
        migrations.AddField(
            model_name='imagesearchlog',
            name='workflow_key',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=160,
                verbose_name='Ключ durable workflow',
            ),
        ),
        migrations.AddField(
            model_name='imagesearchlog',
            name='workflow_slot',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=160,
                verbose_name='Логический слот durable workflow',
            ),
        ),
        migrations.AddField(
            model_name='imagesearchtask',
            name='error_code',
            field=models.CharField(blank=True, editable=False, max_length=80),
        ),
        migrations.AddField(
            model_name='imagesearchtask',
            name='error_message',
            field=models.CharField(blank=True, editable=False, max_length=500),
        ),
        migrations.AddField(
            model_name='imagesearchtask',
            name='finished_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='imagesearchtask',
            name='result',
            field=models.JSONField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='imagesearchtask',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Ожидает'),
                    ('running', 'В работе'),
                    ('succeeded', 'Завершено'),
                    ('failed', 'Ошибка'),
                    (
                        'reconciliation_required',
                        'Требует сверки провайдера',
                    ),
                ],
                db_index=True,
                default='pending',
                max_length=30,
            ),
        ),
        migrations.AddIndex(
            model_name='imagesearchlog',
            index=models.Index(
                fields=['workflow_key', 'workflow_slot'],
                name='img_log_workflow_slot_idx',
            ),
        ),
        migrations.AddConstraint(
            model_name='imagesearchlog',
            constraint=models.UniqueConstraint(
                condition=~models.Q(workflow_key=''),
                fields=('workflow_key', 'workflow_slot'),
                name='uniq_image_log_workflow_slot',
            ),
        ),
        migrations.AddIndex(
            model_name='imagesearchtask',
            index=models.Index(
                fields=['status', '-updated_at'],
                name='img_task_status_updated_idx',
            ),
        ),
    ]
