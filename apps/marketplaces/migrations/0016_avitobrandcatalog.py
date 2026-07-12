from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplaces', '0015_listing_margin_pct'),
    ]

    operations = [
        migrations.CreateModel(
            name='AvitoBrandCatalog',
            fields=[
                ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                ('source_node', models.CharField(max_length=100)),
                ('field_id', models.PositiveIntegerField()),
                ('brands', models.JSONField(default=list)),
                ('synced_at', models.DateTimeField()),
            ],
            options={
                'verbose_name': 'Справочник брендов Avito',
                'verbose_name_plural': 'Справочник брендов Avito',
            },
        ),
    ]
