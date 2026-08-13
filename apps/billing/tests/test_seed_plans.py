import pytest
from django.core.management import call_command

from apps.billing.management.commands.seed_plans import PLANS
from apps.billing.models import Plan


@pytest.mark.django_db
def test_seed_plans_is_idempotent_and_repairs_canonical_values():
    starter = Plan.objects.get(slug=Plan.SLUG_STARTER)
    starter.name = 'Drifted name'
    starter.save(update_fields=['name'])

    call_command('seed_plans', verbosity=0)
    call_command('seed_plans', verbosity=0)

    assert Plan.objects.filter(slug__in=[item['slug'] for item in PLANS]).count() == 4
    starter.refresh_from_db()
    assert starter.name == 'Starter'
