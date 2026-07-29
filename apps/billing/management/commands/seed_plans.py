from django.core.management.base import BaseCommand

from apps.billing.models import Plan

PLANS = [
    {
        'name': 'Starter',
        'slug': 'starter',
        'price_monthly': 4900,
        'price_yearly': 47040,     # полная сумма за 12 месяцев, скидка 20%
        'limit_listings': 1000,
        'limit_sku': 5000,
        'limit_ai_credits': 1000,
    },
    {
        'name': 'Business',
        'slug': 'business',
        'price_monthly': 14900,
        'price_yearly': 143040,
        'limit_listings': 10000,
        'limit_sku': 30000,
        'limit_ai_credits': 5000,
    },
    {
        'name': 'Pro',
        'slug': 'pro',
        'price_monthly': 34900,
        'price_yearly': 335040,
        'limit_listings': 50000,
        'limit_sku': 150000,
        'limit_ai_credits': 20000,
    },
    {
        'name': 'Enterprise',
        'slug': 'enterprise',
        'price_monthly': 79900,
        'price_yearly': 767040,
        'limit_listings': None,
        'limit_sku': None,
        'limit_ai_credits': 50000,
    },
]


class Command(BaseCommand):
    help = 'Загружает начальные тарифные планы MAP'

    def handle(self, *args, **options):
        created, updated = 0, 0
        for data in PLANS:
            _, is_new = Plan.objects.update_or_create(slug=data['slug'], defaults=data)
            if is_new:
                created += 1
            else:
                updated += 1

        self.stdout.write(self.style.SUCCESS(
            f'Готово: создано {created}, обновлено {updated} планов.'
        ))
