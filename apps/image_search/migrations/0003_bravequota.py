from django.db import migrations, models


class Migration(migrations.Migration):
    """Создаёт персистентную таблицу BraveQuota для отслеживания квоты Brave Search API."""

    dependencies = [
        ('image_search', '0002_imagesearchcache_updated_at_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='BraveQuota',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False, verbose_name='ID',
                )),
                ('period', models.CharField(
                    db_index=True, max_length=7, unique=True, verbose_name='Период (YYYY-MM)',
                )),
                ('requests_used', models.PositiveIntegerField(
                    default=0, verbose_name='Использовано запросов',
                )),
                ('limit', models.PositiveIntegerField(
                    default=1000, verbose_name='Лимит по плану',
                )),
                ('cap_notified', models.BooleanField(
                    default=False, verbose_name='Уведомление об исчерпании отправлено',
                )),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Квота Brave Search',
                'verbose_name_plural': 'Квота Brave Search',
            },
        ),
    ]
