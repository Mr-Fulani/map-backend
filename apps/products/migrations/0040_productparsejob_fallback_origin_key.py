from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0039_ingress_idempotency'),
    ]

    operations = [
        migrations.AddField(
            model_name='productparsejob',
            name='fallback_origin_key',
            field=models.CharField(
                blank=True,
                db_index=True,
                editable=False,
                max_length=200,
                verbose_name='Ключ единственного web-research fallback',
            ),
        ),
    ]
