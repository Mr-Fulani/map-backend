# Generated manually for the first persistent model in apps.core.

import uuid

from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='BackgroundJobDispatch',
            fields=[
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Обновлено')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('task_name', models.CharField(max_length=255, verbose_name='Celery task')),
                ('queue', models.CharField(max_length=64, verbose_name='Очередь')),
                ('args', models.JSONField(blank=True, default=list, verbose_name='Позиционные аргументы')),
                ('kwargs', models.JSONField(blank=True, default=dict, verbose_name='Именованные аргументы')),
                ('deduplication_key', models.CharField(blank=True, max_length=255, null=True, unique=True, verbose_name='Ключ дедупликации')),
                ('status', models.CharField(choices=[('pending', 'Ожидает отправки'), ('publishing', 'Отправляется'), ('published', 'Отправлено'), ('running', 'Выполняется'), ('succeeded', 'Завершено'), ('failed', 'Ошибка'), ('cancelled', 'Отменено')], db_index=True, default='pending', max_length=20, verbose_name='Статус')),
                ('available_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='Доступно после')),
                ('claim_token', models.UUIDField(blank=True, editable=False, null=True)),
                ('lease_expires_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('celery_task_id', models.UUIDField(blank=True, editable=False, null=True)),
                ('publish_attempts', models.PositiveIntegerField(default=0)),
                ('run_attempts', models.PositiveIntegerField(default=0)),
                ('max_run_attempts', models.PositiveSmallIntegerField(default=5)),
                ('execution_timeout_seconds', models.PositiveIntegerField(default=3700)),
                ('last_error', models.TextField(blank=True)),
                ('result', models.JSONField(blank=True, null=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'verbose_name': 'Надёжная фоновая задача',
                'verbose_name_plural': 'Надёжные фоновые задачи',
            },
        ),
        migrations.AddIndex(
            model_name='backgroundjobdispatch',
            index=models.Index(fields=['status', 'available_at'], name='core_job_status_due_idx'),
        ),
        migrations.AddIndex(
            model_name='backgroundjobdispatch',
            index=models.Index(fields=['status', 'lease_expires_at'], name='core_job_lease_idx'),
        ),
        migrations.AddIndex(
            model_name='backgroundjobdispatch',
            index=models.Index(fields=['status', 'finished_at'], name='core_job_finished_idx'),
        ),
        migrations.AddIndex(
            model_name='backgroundjobdispatch',
            index=models.Index(fields=['task_name', '-created_at'], name='core_job_task_created_idx'),
        ),
    ]
