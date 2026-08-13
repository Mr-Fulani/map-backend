from collections.abc import Callable

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import BackgroundJobDispatch
from apps.core.paid_search_recovery import (
    PaidSearchCheckpointRecoveryError,
    resume_euroauto_checkpoint,
    resume_image_search_checkpoint,
)


class Command(BaseCommand):
    help = (
        'Revive one exact failed image/Euroauto dispatch whose provider '
        'result is durably safe for provider-free local replay.'
    )

    def add_arguments(self, parser):
        owner = parser.add_mutually_exclusive_group(required=True)
        owner.add_argument('--image-task-id', type=int)
        owner.add_argument('--euroauto-job-id', type=int)
        parser.add_argument('--confirm', required=True)

    def handle(self, *args, **options):
        image_task_id = options.get('image_task_id')
        euroauto_job_id = options.get('euroauto_job_id')
        recovery: Callable[[int], BackgroundJobDispatch]
        if image_task_id is not None:
            expected = f'image:{image_task_id}'
            recovery = resume_image_search_checkpoint
            owner_id = image_task_id
            label = 'image task'
        else:
            if euroauto_job_id is None:  # defensive; argparse group is required
                raise CommandError('--euroauto-job-id is required.')
            expected = f'euroauto:{euroauto_job_id}'
            recovery = resume_euroauto_checkpoint
            owner_id = int(euroauto_job_id)
            label = 'Euroauto job'
        if options['confirm'] != expected:
            raise CommandError(f'--confirm must exactly equal {expected!r}.')
        try:
            dispatch = recovery(owner_id)
        except PaidSearchCheckpointRecoveryError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f'{label} {owner_id} queued as dispatch {dispatch.pk}',
        ))
