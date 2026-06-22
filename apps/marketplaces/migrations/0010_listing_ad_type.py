from django.db import migrations, models


class Migration(migrations.Migration):
    """Добавляет поле «Вид объявления» (AdType) в Listing для фида Avito Autoload."""

    dependencies = [
        ('marketplaces', '0009_listing_status_queued'),
    ]

    operations = [
        migrations.AddField(
            model_name='listing',
            name='ad_type',
            field=models.CharField(
                choices=[
                    ('Товар приобретен на продажу', 'Товар приобретён на продажу — перепродажа (B2B)'),
                    ('Товар от производителя', 'Товар от производителя'),
                    ('Продаю своё', 'Продаю своё — для частников / б/у'),
                ],
                default='Продаю своё',
                max_length=50,
                verbose_name='Вид объявления Avito',
            ),
        ),
    ]
