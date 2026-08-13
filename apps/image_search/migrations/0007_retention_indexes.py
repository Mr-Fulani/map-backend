from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('image_search', '0006_imagesearchtask_dispatch'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='imagesearchlog',
            index=models.Index(fields=['created_at'], name='img_log_created_idx'),
        ),
        migrations.AddIndex(
            model_name='imagesearchtask',
            index=models.Index(fields=['created_at'], name='img_task_created_idx'),
        ),
        migrations.AddIndex(
            model_name='imagesearchcache',
            index=models.Index(fields=['expires_at'], name='img_cache_expires_idx'),
        ),
    ]
