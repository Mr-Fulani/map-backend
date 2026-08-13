from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
        ('image_search', '0005_imagesearchtask'),
    ]

    operations = [
        migrations.AddField(
            model_name='imagesearchtask',
            name='dispatch',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='image_search_requests',
                to='core.backgroundjobdispatch',
            ),
        ),
    ]
