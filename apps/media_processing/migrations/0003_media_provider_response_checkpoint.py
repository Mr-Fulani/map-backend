from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('media_processing', '0002_mediaprocessingjob_request_fingerprint'),
    ]

    operations = [
        migrations.AddField(
            model_name='mediaprocessingjob',
            name='provider_response_apply_claimed_at',
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                editable=False,
                null=True,
                verbose_name='Checkpoint взят в применение',
            ),
        ),
        migrations.AddField(
            model_name='mediaprocessingjob',
            name='provider_response_apply_token',
            field=models.UUIDField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Claim применения ответа провайдера',
            ),
        ),
        migrations.AddField(
            model_name='mediaprocessingjob',
            name='provider_response_digest',
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                verbose_name='SHA256 checkpoint ответа провайдера',
            ),
        ),
        migrations.AddField(
            model_name='mediaprocessingjob',
            name='provider_response_enc',
            field=models.BinaryField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Зашифрованный checkpoint ответа провайдера',
            ),
        ),
        migrations.AddField(
            model_name='mediaprocessingjob',
            name='provider_response_recorded_at',
            field=models.DateTimeField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Ответ провайдера сохранён',
            ),
        ),
        migrations.AddField(
            model_name='mediaprocessingjob',
            name='provider_response_resolved_at',
            field=models.DateTimeField(
                blank=True,
                editable=False,
                null=True,
                verbose_name='Checkpoint ответа провайдера закрыт',
            ),
        ),
        migrations.AddField(
            model_name='mediaprocessingjob',
            name='provider_response_state',
            field=models.CharField(
                blank=True,
                choices=[
                    ('recorded', 'Ответ сохранён'),
                    ('applying', 'Ответ применяется'),
                    ('applied', 'Ответ применён'),
                    ('accounting_resolved', 'Закрыто оператором'),
                ],
                db_index=True,
                editable=False,
                max_length=24,
                verbose_name='Состояние checkpoint ответа провайдера',
            ),
        ),
        migrations.AddField(
            model_name='mediaprocessingjob',
            name='provider_response_status',
            field=models.CharField(
                blank=True,
                choices=[
                    ('succeeded', 'Готово'),
                    ('pending', 'Принято провайдером'),
                    ('failed', 'Отклонено провайдером'),
                ],
                editable=False,
                max_length=20,
                verbose_name='Статус известного ответа провайдера',
            ),
        ),
    ]
