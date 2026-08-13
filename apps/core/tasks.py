from celery import shared_task


@shared_task(queue='notifications')
def purge_retained_data_task():
    from apps.core.retention import purge_retained_data
    return purge_retained_data()


@shared_task(
    queue='notifications',
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def dispatch_due_background_jobs(limit: int = 200):
    """Recover pending deliveries and expired publisher/worker leases."""
    from apps.core.dispatch import publish_due_dispatches
    return publish_due_dispatches(limit=limit)


@shared_task(
    bind=True,
    name='apps.core.tasks.execute_background_dispatch',
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def execute_background_dispatch(self, dispatch_id: str, claim_token: str):
    """Claim one durable delivery before invoking its allowlisted target task."""
    from apps.core.dispatch import claim_dispatch, execute_claimed_dispatch
    dispatch = claim_dispatch(dispatch_id, claim_token)
    if dispatch is None:
        return {'dispatch_id': dispatch_id, 'status': 'duplicate_or_stale'}
    return execute_claimed_dispatch(dispatch)
