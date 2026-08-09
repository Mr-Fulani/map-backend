from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('marketplaces', '0018_avito_category_tree_and_autoload_expiry'),
    ]

    operations = [
        migrations.AddField(
            model_name='listing',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='marketplaceaccount',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
