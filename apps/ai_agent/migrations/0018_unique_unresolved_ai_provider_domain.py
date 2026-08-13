from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ai_agent', '0017_aiprovideroperation'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='aiprovideroperation',
            constraint=models.UniqueConstraint(
                fields=(
                    'tenant', 'task_type', 'domain_type', 'domain_reference',
                ),
                condition=(
                    models.Q(status__in=['reserved', 'pending_reconciliation'])
                    | models.Q(status='settled', apply_state='pending')
                ),
                name='unique_unresolved_ai_provider_domain',
            ),
        ),
    ]
