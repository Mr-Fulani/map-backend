from decimal import Decimal

from django.db import migrations


FULL_YEARLY_PRICES = {
    'starter': Decimal('47040.00'),
    'business': Decimal('143040.00'),
    'pro': Decimal('335040.00'),
    'enterprise': Decimal('767040.00'),
}

LEGACY_MONTHLY_EQUIVALENTS = {
    'starter': Decimal('3920.00'),
    'business': Decimal('11920.00'),
    'pro': Decimal('27920.00'),
    'enterprise': Decimal('63920.00'),
}


def set_full_yearly_prices(apps, schema_editor):
    Plan = apps.get_model('billing', 'Plan')
    for slug, price in FULL_YEARLY_PRICES.items():
        Plan.objects.filter(slug=slug).update(price_yearly=price)


def restore_legacy_prices(apps, schema_editor):
    Plan = apps.get_model('billing', 'Plan')
    for slug, price in LEGACY_MONTHLY_EQUIVALENTS.items():
        Plan.objects.filter(slug=slug).update(price_yearly=price)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0005_invoice_currency'),
    ]

    operations = [
        migrations.RunPython(set_full_yearly_prices, restore_legacy_prices),
    ]
