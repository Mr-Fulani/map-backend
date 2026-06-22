from django.db import migrations, models


def migrate_own_to_resale(apps, schema_editor):
    """Переводит старое значение AdType «Продаю своё» в «Товар приобретен на продажу»."""
    Listing = apps.get_model('marketplaces', 'Listing')
    Listing.objects.filter(ad_type='Продаю своё').update(ad_type='Товар приобретен на продажу')


class Migration(migrations.Migration):
    """Меняет дефолт AdType на «Товар приобретен на продажу» (Avito не принимает «Продаю своё» для запчастей)."""

    dependencies = [
        ('marketplaces', '0010_listing_ad_type'),
    ]

    operations = [
        migrations.RunPython(migrate_own_to_resale, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='listing',
            name='ad_type',
            field=models.CharField(
                choices=[
                    ('Товар приобретен на продажу', 'Товар приобретён на продажу — перепродажа (B2B)'),
                    ('Товар от производителя', 'Товар от производителя'),
                ],
                default='Товар приобретен на продажу',
                max_length=50,
                verbose_name='Вид объявления Avito',
            ),
        ),
    ]
