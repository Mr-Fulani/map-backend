from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('web_research', '0003_market_research'),
    ]

    operations = [
        migrations.AlterField(
            model_name='websearchattempt',
            name='status',
            field=models.CharField(
                choices=[
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
