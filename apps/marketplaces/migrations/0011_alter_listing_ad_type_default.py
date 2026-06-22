from django.db import migrations, models


class Migration(migrations.Migration):
    """Меняет дефолт AdType на «Товар приобретен на продажу» (Avito не принимает «Продаю своё» для запчастей)."""

    dependencies = [
        ('marketplaces', '0010_listing_ad_type'),
    ]

    operations = [
        migrations.AlterField(
            model_name='listing',
            name='ad_type',
            field=models.CharField(
                choices=[
                    ('Товар приобретен на продажу', 'Товар приобретён на продажу — перепродажа (B2B)'),
                    ('Товар от производителя', 'Товар от производителя'),
                    ('Продаю своё', 'Продаю своё — для частников / б/у'),
                ],
                default='Товар приобретен на продажу',
                max_length=50,
                verbose_name='Вид объявления Avito',
            ),
        ),
    ]
