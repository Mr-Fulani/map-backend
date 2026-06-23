from django.db import migrations, models


class Migration(migrations.Migration):
    """Добавляет промежуточный статус «Снимается» (archiving) для честного снятия с публикации."""

    dependencies = [
        ('marketplaces', '0012_marketplaceaccount_autoload_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='listing',
            name='status',
            field=models.CharField(
                choices=[
                    ('draft', 'Черновик'),
                    ('queued', 'В очереди'),
                    ('pending', 'На модерации Avito'),
                    ('active', 'Активно'),
                    ('rejected', 'Отклонено'),
                    ('archiving', 'Снимается (ждёт Avito)'),
                    ('archived', 'В архиве'),
                    ('deleted', 'Удалено'),
                    ('requires_review', 'Требует проверки'),
                    ('limit_reached', 'Лимит достигнут'),
                ],
                default='draft',
                max_length=20,
                verbose_name='Статус',
            ),
        ),
    ]
