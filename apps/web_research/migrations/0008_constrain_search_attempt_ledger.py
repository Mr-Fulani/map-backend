from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('web_research', '0007_finalize_search_attempt_fields'),
    ]

    # Build indexes and uniqueness fences only after the field-alteration
    # transaction commits, so PostgreSQL has no pending FK trigger events.
    operations = [
        migrations.AddIndex(
            model_name='websearchattempt',
            index=models.Index(
                fields=['tenant', '-created_at'],
                name='websearch_tenant_recent_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='websearchattempt',
            index=models.Index(
                fields=['tenant', 'operation', 'domain_reference'],
                name='websearch_domain_fence_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='websearchattempt',
            index=models.Index(
                fields=['workflow', 'created_at'],
                name='websearch_workflow_call_idx',
            ),
        ),
        migrations.AddIndex(
            model_name='websearchattempt',
            index=models.Index(
                fields=['reconciliation_state', 'apply_state', 'updated_at'],
                name='websearch_retention_idx',
            ),
        ),
        migrations.AddConstraint(
            model_name='websearchattempt',
            constraint=models.UniqueConstraint(
                fields=('workflow', 'call_key'),
                name='uniq_websearch_workflow_call',
            ),
        ),
        migrations.AddConstraint(
            model_name='websearchworkflow',
            constraint=models.UniqueConstraint(
                fields=('tenant', 'operation', 'workflow_key'),
                name='uniq_websearch_workflow_key',
            ),
        ),
        migrations.AddConstraint(
            model_name='websearchworkflow',
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ('status__in', ['in_progress', 'apply_pending', 'uncertain']),
                ),
                fields=('tenant', 'operation', 'domain_reference'),
                name='uniq_active_websearch_domain',
            ),
        ),
    ]
