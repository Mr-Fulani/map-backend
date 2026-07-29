import calendar
from datetime import datetime, time

from django.db import migrations
from django.utils import timezone


def add_months(anchor, months):
    absolute_month = anchor.year * 12 + anchor.month - 1 + months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def current_ai_period(subscription, today):
    start = subscription.current_period_start
    end = subscription.current_period_end
    if subscription.billing_period != 'yearly' or not (start <= today < end):
        return start, end

    period_start = start
    for month_index in range(1, 13):
        period_end = min(add_months(start, month_index), end)
        if today < period_end:
            return period_start, period_end
        period_start = period_end
        if period_end >= end:
            break
    return start, end


def backfill_ai_periods(apps, schema_editor):
    Subscription = apps.get_model('billing', 'Subscription')
    AIWallet = apps.get_model('billing', 'AIWallet')
    today = timezone.localdate()

    for subscription in Subscription.objects.all().iterator():
        period_start, period_end = current_ai_period(subscription, today)
        Subscription.objects.filter(pk=subscription.pk).update(
            ai_period_start=period_start,
            ai_period_end=period_end,
        )
        AIWallet.objects.filter(tenant_id=subscription.tenant_id).update(
            included_expires_at=timezone.make_aware(
                datetime.combine(period_end, time.max),
            ),
        )


def restore_subscription_period_expiry(apps, schema_editor):
    Subscription = apps.get_model('billing', 'Subscription')
    AIWallet = apps.get_model('billing', 'AIWallet')
    for subscription in Subscription.objects.all().iterator():
        AIWallet.objects.filter(tenant_id=subscription.tenant_id).update(
            included_expires_at=timezone.make_aware(
                datetime.combine(subscription.current_period_end, time.max),
            ),
        )
    Subscription.objects.update(ai_period_start=None, ai_period_end=None)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0007_subscription_ai_period_end_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_ai_periods, restore_subscription_period_expiry),
    ]
