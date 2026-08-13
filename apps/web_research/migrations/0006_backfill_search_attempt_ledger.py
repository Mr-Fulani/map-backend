import hashlib
import json

from django.db import migrations


def backfill_attempt_ownership(apps, schema_editor):
    Attempt = apps.get_model('web_research', 'WebSearchAttempt')
    attempts = Attempt.objects.select_related('run').order_by('created_at', 'pk')
    for attempt in attempts.iterator(chunk_size=1000):
        if attempt.run_id is None:
            raise RuntimeError(
                'Legacy WebSearchAttempt without its required run cannot be migrated.'
            )
        attempt.tenant_id = attempt.run.tenant_id
        attempt.operation = 'web_research'
        attempt.call_kind = 'text'
        purpose_family = (
            'pricing'
            if attempt.run.purpose in {'pricing', 'combined'}
            else 'enrichment'
        )
        stable_reference = (
            f'product:{attempt.run.product_id}:purpose:{purpose_family}'
        )
        attempt.domain_reference = stable_reference
        if attempt.status == 'outcome_uncertain':
            attempt.reconciliation_state = 'pending'
        attempt.save(update_fields=[
            'tenant', 'operation', 'call_kind', 'domain_reference',
            'reconciliation_state',
        ])


def backfill_attempt_workflows(apps, schema_editor):
    Attempt = apps.get_model('web_research', 'WebSearchAttempt')
    Workflow = apps.get_model('web_research', 'WebSearchWorkflow')
    attempts = Attempt.objects.select_related('run').order_by('created_at', 'pk')
    for attempt in attempts.iterator(chunk_size=1000):
        pending = attempt.reconciliation_state == 'pending'
        resolved = attempt.reconciliation_state == 'resolved'
        if pending:
            workflow_status = 'uncertain'
            apply_state = 'pending'
        elif resolved:
            workflow_status = 'reconciled'
            apply_state = 'applied'
        else:
            # Historical successful rows predate provider checkpoints, but
            # their domain records were already committed by the old caller.
            workflow_status = 'applied'
            apply_state = 'applied'
        canonical_reference = attempt.domain_reference
        snapshot = (
            {'legacy_domain': canonical_reference}
            if pending else {'legacy_attempt_id': attempt.pk}
        )
        encoded = json.dumps(
            snapshot, ensure_ascii=False, separators=(',', ':'), sort_keys=True,
        ).encode('utf-8')
        workflow = None
        if pending:
            workflow = Workflow.objects.filter(
                tenant_id=attempt.tenant_id,
                operation=attempt.operation,
                domain_reference=canonical_reference,
                status='uncertain',
            ).first()
        if workflow is None:
            workflow = Workflow.objects.create(
                tenant_id=attempt.tenant_id,
                product_id=attempt.run.product_id if attempt.run_id else None,
                run_id=attempt.run_id,
                operation=attempt.operation,
                domain_reference=canonical_reference,
                workflow_key=(
                    f'legacy-domain:{attempt.pk}'
                    if pending else f'legacy-attempt:{attempt.pk}'
                ),
                input_fingerprint=hashlib.sha256(encoded).hexdigest(),
                input_snapshot=snapshot,
                status=workflow_status,
                applied_at=attempt.updated_at if workflow_status == 'applied' else None,
                reconciliation_action=attempt.reconciliation_action,
                reconciliation_note=attempt.reconciliation_note,
                reconciled_at=attempt.reconciled_at,
            )
        request_payload = {
            'provider_id': attempt.provider_id,
            'call_kind': attempt.call_kind,
            'query': attempt.query,
        }
        request_encoded = json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')
        attempt.workflow_id = workflow.pk
        attempt.call_key = f'legacy:{attempt.pk}'
        attempt.request_fingerprint = hashlib.sha256(request_encoded).hexdigest()
        attempt.apply_state = apply_state
        attempt.save(update_fields=[
            'workflow', 'call_key', 'request_fingerprint', 'apply_state',
        ])


class Migration(migrations.Migration):

    dependencies = [
        ('web_research', '0005_search_attempt_ledger'),
    ]

    # The nullable schema in 0005 must commit before this migration updates
    # legacy FK rows. The NOT NULL and index phases follow in 0007/0008.
    operations = [
        migrations.RunPython(
            backfill_attempt_ownership,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(
            backfill_attempt_workflows,
            migrations.RunPython.noop,
        ),
    ]
