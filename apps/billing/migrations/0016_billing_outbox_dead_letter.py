from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0015_checkout_intent_keys'),
    ]

    operations = [
        migrations.AddField(
            model_name='billingoutboxevent',
            name='dead_lettered_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='billingoutboxevent',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Ожидает отправки'),
                    ('processing', 'Отправляется'),
                    ('dispatched', 'Отправлено брокеру'),
                    ('dead', 'Требует ручного разбора'),
                ],
                default='pending',
                max_length=20,
            ),
        ),
    ]
