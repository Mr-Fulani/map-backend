from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from apps.ai_agent.models import AIProviderOperation, AITaskType
from apps.core.models import BackgroundJobDispatch
from apps.tenants.models import Tenant
from apps.web_research.models import (
    WebResearchRun,
    WebSearchAttempt,
    WebSearchWorkflow,
)


class Command(BaseCommand):
    help = (
        'Requeue one exact web-research run whose paid search checkpoint '
        'is durably waiting for local apply.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--run-id', required=True, type=int)
        parser.add_argument('--confirm', required=True)

    def handle(self, *args, **options):
        run_id = options['run_id']
        if options['confirm'] != str(run_id):
            raise CommandError('--confirm must exactly match --run-id.')

        from apps.web_research.tasks import enqueue_web_research_run

        with transaction.atomic():
            try:
                tenant_id = WebResearchRun.objects.values_list(
                    'tenant_id', flat=True,
                ).get(pk=run_id)
            except WebResearchRun.DoesNotExist as exc:
                raise CommandError('Web research run was not found.') from exc
            Tenant.objects.select_for_update().only('pk').get(pk=tenant_id)
            run = WebResearchRun.objects.select_for_update().get(pk=run_id)
            workflow = WebSearchWorkflow.objects.select_for_update().filter(
                tenant_id=tenant_id,
                run_id=run_id,
                operation='web_research',
                status__in=[
                    WebSearchWorkflow.Status.IN_PROGRESS,
                    WebSearchWorkflow.Status.APPLY_PENDING,
                    WebSearchWorkflow.Status.APPLIED,
                ],
            ).first()
            if workflow is None:
                raise CommandError(
                    'Run has no paid search checkpoint waiting for apply.',
                )
            attempts = workflow.attempts.select_for_update()
            if workflow.status == WebSearchWorkflow.Status.IN_PROGRESS and (
                attempts.filter(
                    Q(
                        reconciliation_state=(
                            WebSearchAttempt.ReconciliationState.PENDING
                        ),
                    )
                    | Q(status__in=[
                        WebSearchAttempt.Status.STARTED,
                        WebSearchAttempt.Status.OUTCOME_UNCERTAIN,
                    ])
                ).exists()
                or attempts.exclude(
                    status__in=[
                        WebSearchAttempt.Status.FAILED,
                        WebSearchAttempt.Status.SKIPPED,
                    ],
                    reconciliation_state=(
                        WebSearchAttempt.ReconciliationState.NOT_REQUIRED
                    ),
                    apply_state=WebSearchAttempt.ApplyState.PENDING,
                ).exists()
            ):
                raise CommandError(
                    'In-progress search plan is not safe for automatic replay.',
                )
            if workflow.status == WebSearchWorkflow.Status.APPLY_PENDING and (
                not attempts.filter(
                    status__in=[
                        WebSearchAttempt.Status.SUCCESS,
                        WebSearchAttempt.Status.EMPTY,
                    ],
                    apply_state=WebSearchAttempt.ApplyState.PENDING,
                    checkpoint_enc__isnull=False,
                ).exists()
                or attempts.filter(
                    reconciliation_state=(
                        WebSearchAttempt.ReconciliationState.PENDING
                    ),
                ).exists()
            ):
                raise CommandError(
                    'Run checkpoint is not safe for automatic local replay.',
                )
            if workflow.status == WebSearchWorkflow.Status.APPLIED and (
                (
                    attempts.exists() and not attempts.filter(
                        status__in=[
                            WebSearchAttempt.Status.SUCCESS,
                            WebSearchAttempt.Status.EMPTY,
                        ],
                        checkpoint_enc__isnull=False,
                    ).exists()
                )
                or (
                    not attempts.exists()
                    and run.status not in {
                        WebResearchRun.Status.QUEUED,
                        WebResearchRun.Status.RUNNING,
                    }
                )
                or
                attempts.filter(
                    reconciliation_state=(
                        WebSearchAttempt.ReconciliationState.PENDING
                    ),
                ).exists()
                or attempts.exclude(
                    apply_state=WebSearchAttempt.ApplyState.APPLIED,
                ).exists()
            ):
                raise CommandError(
                    'Applied search evidence is not safe for local replay.',
                )
            if AIProviderOperation.objects.select_for_update().filter(
                tenant_id=tenant_id,
                task_type=AITaskType.WEB_RESEARCH,
                domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
                domain_reference=str(run_id),
                status__in=[
                    AIProviderOperation.Status.RESERVED,
                    AIProviderOperation.Status.PENDING_RECONCILIATION,
                ],
            ).exists():
                raise CommandError(
                    'Run has an unresolved AI provider operation.',
                )
            run.status = WebResearchRun.Status.QUEUED
            run.finished_at = None
            run.save(update_fields=['status', 'finished_at', 'updated_at'])
            dispatch = enqueue_web_research_run(
                run.pk,
                revive_failed=True,
            )
            if dispatch.status not in {
                BackgroundJobDispatch.Status.PENDING,
                BackgroundJobDispatch.Status.PUBLISHING,
                BackgroundJobDispatch.Status.PUBLISHED,
                BackgroundJobDispatch.Status.RUNNING,
            }:
                raise CommandError(
                    'Canonical dispatch is terminal and cannot be revived safely.',
                )
        self.stdout.write(self.style.SUCCESS(
            f'web research run {run.pk} queued as dispatch {dispatch.pk}',
        ))
