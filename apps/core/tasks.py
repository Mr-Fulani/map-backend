from celery import shared_task


@shared_task(queue='notifications')
def purge_retained_data_task():
    from apps.core.retention import purge_retained_data
    return purge_retained_data()
