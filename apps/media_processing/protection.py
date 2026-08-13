from django.db.models import Q


UNRESOLVED_MEDIA_JOB_STATUSES = ('queued', 'submitted', 'processing')


def unresolved_media_job_q() -> Q:
    """Jobs whose provider outcome or wallet reservation is not final."""
    return (
        Q(status__in=UNRESOLVED_MEDIA_JOB_STATUSES)
        | Q(error_code='outcome_uncertain')
        | Q(provider_metadata__credit_reservation__status='reserved')
        | Q(provider_response_state__in=['recorded', 'applying'])
    )
