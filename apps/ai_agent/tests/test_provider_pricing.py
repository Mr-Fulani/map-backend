from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.ai_agent.models import AIModel, AIProviderPrice, AITaskType


def make_model(external_id: str = 'pricing-test-model') -> AIModel:
    return AIModel.objects.create(
        provider=AIModel.PROVIDER_OPENAI,
        external_id=external_id,
        display_name='Pricing test model',
        supported_tasks=[AITaskType.DESCRIPTION],
    )


def make_price(model, effective_from, **overrides) -> AIProviderPrice:
    values = {
        'model': model,
        'currency': 'USD',
        'input_per_million': Decimal('1.25'),
        'cached_read_per_million': Decimal('0.125'),
        'cached_write_per_million': Decimal('1.50'),
        'output_per_million': Decimal('10'),
        'effective_from': effective_from,
    }
    values.update(overrides)
    return AIProviderPrice.objects.create(**values)


@pytest.mark.django_db
def test_effective_price_is_resolved_at_request_time():
    model = make_model()
    now = timezone.now()
    old_price = make_price(model, now - timedelta(days=30))
    new_price = make_price(
        model,
        now + timedelta(days=1),
        input_per_million=Decimal('2.50'),
    )

    assert model.provider_price_at(now) == old_price
    assert model.provider_price_at(now + timedelta(days=2)) == new_price
    assert model.provider_price_at(now - timedelta(days=31)) is None


@pytest.mark.django_db
def test_new_price_does_not_mutate_historical_version():
    model = make_model()
    now = timezone.now()
    old_price = make_price(model, now - timedelta(days=1))
    make_price(
        model,
        now,
        currency='EUR',
        input_per_million=Decimal('2'),
    )

    old_price.refresh_from_db()
    assert old_price.currency == 'USD'
    assert old_price.input_per_million == Decimal('1.25000000')


@pytest.mark.django_db
def test_existing_price_version_cannot_be_edited():
    price = make_price(make_model(), timezone.now())
    price.output_per_million = Decimal('99')

    with pytest.raises(ValidationError, match='неизменяема'):
        price.save()


@pytest.mark.django_db
def test_historical_price_cannot_be_bulk_updated_or_deleted():
    price = make_price(make_model(), timezone.now())
    prices = AIProviderPrice.objects.filter(pk=price.pk)

    with pytest.raises(ValidationError, match='неизменяемы'):
        prices.update(output_per_million=Decimal('99'))

    with pytest.raises(ValidationError, match='нельзя удалять'):
        prices.delete()

    with pytest.raises(ValidationError, match='нельзя удалять'):
        price.delete()


@pytest.mark.django_db
def test_price_supports_iso_currency_and_all_token_components():
    price = make_price(
        make_model(),
        timezone.now(),
        currency='EUR',
    )

    cost = price.calculate_cost(
        input_tokens=1_000_000,
        cached_read_tokens=200_000,
        cached_write_tokens=100_000,
        output_tokens=50_000,
    )

    assert price.currency == 'EUR'
    assert cost == Decimal('1.55000000')


@pytest.mark.django_db
def test_currency_must_be_uppercase_iso_code():
    with pytest.raises(ValidationError):
        make_price(
            make_model(),
            timezone.now(),
            currency='usd',
        )


@pytest.mark.django_db
def test_negative_provider_price_is_rejected_by_database():
    model = make_model()

    with pytest.raises((ValidationError, IntegrityError)):
        with transaction.atomic():
            make_price(
                model,
                timezone.now(),
                output_per_million=Decimal('-0.01'),
            )
