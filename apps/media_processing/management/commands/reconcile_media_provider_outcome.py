from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.timezone import now

from apps.media_processing.models import MediaProcessingJob
from apps.media_processing.services import (
    MediaProviderCheckpointApplyInProgress,
    MediaProviderCheckpointNotApplicable,
    MediaProviderOutcomeUncertain,
    _release_job_credits,
    _settle_job_credits,
    apply_checkpointed_provider_result,
    mark_provider_checkpoint_accounting_resolved,
)


class Command(BaseCommand):
    help = (
        'Resolve exactly one media provider outcome after an operator checks '
        'the provider dashboard. Never retries or resubmits the operation.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--job-id', type=int, required=True)
        parser.add_argument(
            '--action',
            choices=['apply-known', 'release', 'settle-reserved'],
            required=True,
        )
        parser.add_argument('--note', required=True)
        parser.add_argument('--confirm-job-id', type=int, required=True)

    def handle(self, *args, **options):
        job_id = options['job_id']
        if job_id < 1 or options['confirm_job_id'] != job_id:
            raise CommandError('--confirm-job-id must exactly match --job-id.')
        note = ' '.join(str(options['note'] or '').split())
        if not 10 <= len(note) <= 500:
            raise CommandError('--note must contain 10..500 non-control characters.')

        action = options['action']
        if action == 'apply-known':
            self._apply_known_response(job_id, note)
            return
        self._resolve_accounting(job_id, action, note)

    def _apply_known_response(self, job_id: int, note: str) -> None:
        try:
            job = MediaProcessingJob.objects.select_related('tenant').get(pk=job_id)
        except MediaProcessingJob.DoesNotExist as exc:
            raise CommandError('Media job does not exist.') from exc
        reconciliation = dict((job.provider_metadata or {}).get('reconciliation') or {})
        if reconciliation.get('status') == 'resolved':
            if reconciliation.get('action') != 'apply-known':
                raise CommandError('Job was already resolved with a different action.')
            self.stdout.write(f'media provider outcome already resolved: job={job.pk}')
            return
        if job.provider_response_state not in {
            MediaProcessingJob.ProviderResponseState.RECORDED,
            MediaProcessingJob.ProviderResponseState.APPLYING,
            MediaProcessingJob.ProviderResponseState.APPLIED,
        }:
            raise CommandError('Job has no applicable known provider response checkpoint.')
        try:
            apply_checkpointed_provider_result(job)
        except MediaProviderCheckpointNotApplicable as exc:
            raise CommandError(
                'Known provider response is bounded but cannot be applied; '
                'verify the provider dashboard and use explicit accounting.',
            ) from exc
        except MediaProviderCheckpointApplyInProgress as exc:
            raise CommandError(
                'Known provider response is already being applied; retry after the lease.',
            ) from exc
        except MediaProviderOutcomeUncertain as exc:
            raise CommandError(
                'Known provider response was not applied; checkpoint remains pending.',
            ) from exc
        except Exception as exc:
            raise CommandError(
                'Known provider response was not applied; checkpoint remains pending.',
            ) from exc

        with transaction.atomic():
            locked = MediaProcessingJob.objects.select_for_update().get(pk=job_id)
            metadata = dict(locked.provider_metadata or {})
            reconciliation = dict(metadata.get('reconciliation') or {})
            if reconciliation.get('status') == 'resolved':
                if reconciliation.get('action') != 'apply-known':
                    raise CommandError('Job was already resolved with a different action.')
            else:
                metadata['reconciliation'] = {
                    'status': 'resolved',
                    'action': 'apply-known',
                    'note': note,
                    'resolved_at': now().isoformat(),
                }
                locked.provider_metadata = metadata
                locked.save(update_fields=['provider_metadata', 'updated_at'])
        self.stdout.write(self.style.SUCCESS(
            f'media provider outcome resolved: job={job.pk} action=apply-known',
        ))

    @transaction.atomic
    def _resolve_accounting(self, job_id: int, action: str, note: str) -> None:
        try:
            job = (
                MediaProcessingJob.objects.select_for_update()
                .select_related('tenant')
                .get(pk=job_id)
            )
        except MediaProcessingJob.DoesNotExist as exc:
            raise CommandError('Media job does not exist.') from exc

        provider_metadata = dict(job.provider_metadata or {})
        reconciliation = dict(provider_metadata.get('reconciliation') or {})
        if reconciliation.get('status') == 'resolved':
            if reconciliation.get('action') != action:
                raise CommandError('Job was already resolved with a different action.')
            self.stdout.write(f'media provider outcome already resolved: job={job.pk}')
            return
        if (
            job.status != MediaProcessingJob.Status.FAILED
            or job.error_code != 'outcome_uncertain'
        ):
            raise CommandError('Job is not awaiting uncertain-outcome reconciliation.')
        if (
            job.provider_response_state
            == MediaProcessingJob.ProviderResponseState.APPLYING
        ):
            raise CommandError(
                'Known provider response is being applied; accounting is unsafe.',
            )
        if job.provider_response_state not in {
            '',
            MediaProcessingJob.ProviderResponseState.RECORDED,
        }:
            raise CommandError(
                'Provider response checkpoint is already terminal.',
            )

        known_status = job.provider_response_status
        if (
            action == 'release'
            and known_status in {
                MediaProcessingJob.ProviderResponseStatus.PENDING,
                MediaProcessingJob.ProviderResponseStatus.SUCCEEDED,
            }
        ):
            raise CommandError(
                'Known provider response proves acceptance; release is unsafe.',
            )
        if (
            action == 'settle-reserved'
            and known_status == MediaProcessingJob.ProviderResponseStatus.FAILED
        ):
            raise CommandError(
                'Known provider response proves rejection; settlement is unsafe.',
            )

        credit_status = str(
            (provider_metadata.get('credit_reservation') or {}).get('status') or '',
        )
        if action == 'release' and credit_status == 'settled':
            raise CommandError(
                'Credits were already settled; release would contradict the wallet audit.',
            )
        if action == 'settle-reserved' and credit_status == 'released':
            raise CommandError(
                'Credits were already released; settlement would contradict the wallet audit.',
            )

        if action == 'release':
            _release_job_credits(job, reason='operator_confirmed_not_accepted')
        else:
            _settle_job_credits(job)
        mark_provider_checkpoint_accounting_resolved(job)

        metadata = dict(job.provider_metadata or {})
        metadata['reconciliation'] = {
            'status': 'resolved',
            'action': action,
            'note': note,
            'resolved_at': now().isoformat(),
        }
        job.provider_metadata = metadata
        job.error_code = f'outcome_reconciled_{action.replace("-", "_")}'
        job.error_message = 'Неопределённый результат сверен оператором.'
        job.save(update_fields=[
            'provider_metadata', 'error_code', 'error_message', 'updated_at',
        ])
        self.stdout.write(self.style.SUCCESS(
            f'media provider outcome resolved: job={job.pk} action={action}',
        ))
