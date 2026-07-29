from datetime import datetime, time
from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def seed_wallets_and_packages(apps, schema_editor):
    AICreditPackage = apps.get_model('billing', 'AICreditPackage')
    AICreditTransaction = apps.get_model('billing', 'AICreditTransaction')
    AIWallet = apps.get_model('billing', 'AIWallet')
    Subscription = apps.get_model('billing', 'Subscription')
    Tenant = apps.get_model('tenants', 'Tenant')

    packages = [
        ('1 000 AI-кредитов', '1000', '990', 10),
        ('5 000 AI-кредитов', '5000', '4500', 20),
        ('20 000 AI-кредитов', '20000', '16000', 30),
    ]
    for name, credits, price_rub, sort_order in packages:
        AICreditPackage.objects.update_or_create(
            name=name,
            defaults={
                'credits': credits,
                'price_rub': price_rub,
                'sort_order': sort_order,
                'is_active': True,
            },
        )

    subscriptions = {
        sub.tenant_id: sub
        for sub in Subscription.objects.select_related('plan').all()
    }
    for tenant in Tenant.objects.all().iterator():
        sub = subscriptions.get(tenant.pk)
        included = Decimal('0')
        expires_at = None
        if sub and sub.plan.limit_ai_credits is not None:
            included = max(
                Decimal('0'),
                Decimal(sub.plan.limit_ai_credits) - Decimal(tenant.ai_credits_used),
            )
            expires_at = timezone.make_aware(
                datetime.combine(sub.current_period_end, time.max),
            )
        wallet, created = AIWallet.objects.get_or_create(
            tenant=tenant,
            defaults={
                'included_balance': included,
                'included_expires_at': expires_at,
            },
        )
        if created and included:
            AICreditTransaction.objects.create(
                wallet=wallet,
                tenant=tenant,
                kind='grant',
                balance_type='included',
                amount=included,
                idempotency_key=f'wallet-bootstrap:{tenant.pk}',
                reference='legacy-ai-credits',
            )


def unseed_packages(apps, schema_editor):
    AICreditPackage = apps.get_model('billing', 'AICreditPackage')
    AICreditPackage.objects.filter(
        name__in=[
            '1 000 AI-кредитов',
            '5 000 AI-кредитов',
            '20 000 AI-кредитов',
        ],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0003_aicreditpackage_invoice_metadata_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_wallets_and_packages, unseed_packages),
    ]
