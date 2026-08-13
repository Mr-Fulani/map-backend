from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('image_search', '0008_imagesearchintent'),
    ]

    operations = [
        migrations.AddField(
            model_name='imagesearchlog',
            name='error_code',
            field=models.CharField(blank=True, max_length=80, verbose_name='Код ошибки'),
        ),
        migrations.AddField(
            model_name='imagesearchlog',
            name='outcome',
            field=models.CharField(
                choices=[
                    ('unknown', 'Не классифицировано (legacy)'),
                    ('completed', 'Завершено'),
                    ('safe_failure', 'Безопасный отказ'),
                    ('outcome_uncertain', 'Результат провайдера неизвестен'),
                ],
                default='unknown',
                max_length=20,
                verbose_name='Результат запроса к провайдеру',
            ),
        ),
    ]
