import django.db.models.deletion

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('web_research', '0006_backfill_search_attempt_ledger'),
    ]

    # Legacy rows were populated in 0006. Commit that work before enforcing
    # NOT NULL foreign keys and final field definitions.
    operations = [
        migrations.AlterField(
            model_name='websearchattempt',
            name='tenant',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='web_search_attempts',
                to='tenants.tenant',
                verbose_name='Тенант',
            ),
        ),
        migrations.AlterField(
            model_name='websearchattempt',
            name='call_key',
            field=models.CharField(
                max_length=160,
                verbose_name='Детерминированный ключ вызова',
            ),
        ),
        migrations.AlterField(
            model_name='websearchattempt',
            name='request_fingerprint',
            field=models.CharField(
                max_length=64,
                verbose_name='Отпечаток запроса',
            ),
        ),
        migrations.AlterField(
            model_name='websearchattempt',
            name='workflow',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='attempts',
                to='web_research.websearchworkflow',
                verbose_name='Рабочий процесс',
            ),
        ),
        migrations.AlterField(
            model_name='websearchattempt',
            name='status',
            field=models.CharField(
                choices=[
                    ('started', 'Запрос начат'),
                    ('success', 'Успешно'),
                    ('empty', 'Нет результатов'),
                    ('failed', 'Ошибка'),
                    ('outcome_uncertain', 'Результат провайдера неизвестен'),
                    ('skipped', 'Пропущено'),
                ],
                max_length=20,
                verbose_name='Статус',
            ),
        ),
    ]
