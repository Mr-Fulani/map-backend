from decimal import Decimal

from django.db import migrations


ENTERPRISE_AI_CREDITS = 50_000


def limit_enterprise_ai_credits(apps, schema_editor):
    AICreditTransaction = apps.get_model('billing', 'AICreditTransaction')
    AIWallet = apps.get_model('billing', 'AIWallet')
    Plan = apps.get_model('billing', 'Plan')
    Subscription = apps.get_model('billing', 'Subscription')

    Plan.objects.filter(slug='enterprise').update(
        limit_ai_credits=ENTERPRISE_AI_CREDITS,
    )

    subscriptions = {
        subscription.tenant_id: subscription
        for subscription in Subscription.objects.select_related('plan').all()
    }
    for wallet in AIWallet.objects.select_related('tenant').iterator():
        subscription = subscriptions.get(wallet.tenant_id)
        if subscription is None:
            wallet.included_limit = Decimal('0')
            wallet.save(update_fields=['included_limit'])
            continue

        plan_limit = subscription.plan.limit_ai_credits
        effective_limit = (
            wallet.tenant.ai_credit_limit_override
            if wallet.tenant.ai_credit_limit_override is not None
            else plan_limit
        )
        effective_limit = Decimal(effective_limit or 0)
        wallet.included_limit = effective_limit

        if subscription.plan.slug == 'enterprise':
            adjustment = effective_limit - wallet.included_balance
            wallet.included_balance = effective_limit
            wallet.notification_state = {}
            if adjustment:
                AICreditTransaction.objects.create(
                    wallet=wallet,
                    tenant_id=wallet.tenant_id,
                    kind='adjustment',
                    balance_type='included',
                    amount=adjustment,
                    idempotency_key=f'enterprise-limit-migration:{wallet.tenant_id}',
                    reference='enterprise-monthly-limit',
                    details={'new_limit': str(effective_limit)},
                )

        wallet.save(update_fields=[
            'included_limit', 'included_balance', 'notification_state',
        ])


def restore_enterprise_unlimited_state(apps, schema_editor):
    AIWallet = apps.get_model('billing', 'AIWallet')
    Plan = apps.get_model('billing', 'Plan')
    Subscription = apps.get_model('billing', 'Subscription')

    enterprise_tenant_ids = Subscription.objects.filter(
        plan__slug='enterprise',
    ).values_list('tenant_id', flat=True)
    AIWallet.objects.filter(tenant_id__in=enterprise_tenant_ids).update(
        included_limit=Decimal('0'),
        included_balance=Decimal('0'),
        notification_state={},
    )
    Plan.objects.filter(slug='enterprise').update(limit_ai_credits=None)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0009_aiwallet_included_limit_aiwallet_notification_state'),
        ('tenants', '0011_tenant_ai_credit_limit_override'),
    ]

    operations = [
        migrations.RunPython(
            limit_enterprise_ai_credits,
            restore_enterprise_unlimited_state,
        ),
    ]
