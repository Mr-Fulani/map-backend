import django.db.models
from django.db import migrations, models


def require_unique_payment_ids(apps, schema_editor):
    invoice_model = apps.get_model('billing', 'Invoice')
    database_alias = schema_editor.connection.alias
    duplicates = (
        invoice_model.objects.using(database_alias)
        .exclude(yookassa_payment_id='')
        .values('yookassa_payment_id')
        .annotate(row_count=django.db.models.Count('pk'))
        .filter(row_count__gt=1)
    )
    duplicate_count = duplicates.count()
    if duplicate_count:
        raise RuntimeError(
            'Нельзя добавить уникальность yookassa_payment_id: '
            f'найдено дублирующихся идентификаторов: {duplicate_count}. '
            'Остановите миграцию и вручную разрешите каждый конфликт Invoice.',
        )


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0011_invoice_refund_review_required_and_more'),
    ]

    operations = [
        migrations.RunPython(
            require_unique_payment_ids,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name='invoice',
            constraint=models.UniqueConstraint(
                condition=~models.Q(yookassa_payment_id=''),
                fields=('yookassa_payment_id',),
                name='unique_nonempty_yookassa_payment_id',
            ),
        ),
        migrations.AddField(
            model_name='billingwebhookevent',
            name='processing_started_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='billingwebhookevent',
            name='processing_token',
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
    ]
