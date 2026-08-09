from decimal import Decimal

import pytest
from django.test import Client, override_settings

from apps.ai_agent.models import AIModel, AITaskType, TenantAITaskModel
from apps.billing.ai_wallet import AIWalletService
from apps.tenants.tests.auth import (
    create_tenant_with_operator_key,
    owner_access_token,
)


def make_tenant(slug: str):
    return create_tenant_with_operator_key(
        name=slug,
        slug=slug,
        owner_email=f'{slug}@test.com',
        owner_password='pass12345',
    )


@pytest.mark.django_db
def test_trial_grants_included_credits_and_topup_survives_period_reset():
    tenant, _api_key = make_tenant('wallet-period-co')
    initial = AIWalletService.summary(tenant)

    assert initial['included'] == Decimal(tenant.subscription.plan.limit_ai_credits)
    assert initial['purchased'] == Decimal('0')

    AIWalletService.topup(
        tenant,
        Decimal('1000'),
        idempotency_key='test-topup:wallet-period-co',
    )
    AIWalletService.grant_included(
        tenant,
        Decimal('500'),
        period_end=tenant.subscription.current_period_end,
        idempotency_key='test-period-reset:wallet-period-co',
    )

    summary = AIWalletService.summary(tenant)
    assert summary['included'] == Decimal('500')
    assert summary['purchased'] == Decimal('1000')


@pytest.mark.django_db
def test_reservation_is_settled_at_actual_usage():
    tenant, _api_key = make_tenant('wallet-settle-co')
    before = AIWalletService.summary(tenant)['available']

    reservation = AIWalletService.reserve(
        tenant,
        Decimal('20'),
        key='test-reservation:wallet-settle-co',
    )
    reserved = AIWalletService.summary(tenant)
    assert reserved['reserved'] == Decimal('20')
    assert reserved['available'] == before - Decimal('20')

    charged = AIWalletService.settle(tenant, reservation, Decimal('7.5'))
    after = AIWalletService.summary(tenant)
    assert charged == Decimal('7.5')
    assert after['reserved'] == Decimal('0')
    assert after['available'] == before - Decimal('7.5')


@pytest.mark.django_db
def test_settlement_does_not_consume_another_requests_reservation():
    tenant, _api_key = make_tenant('wallet-parallel-co')
    wallet = AIWalletService.ensure_wallet(tenant)
    wallet.included_balance = Decimal('100')
    wallet.save(update_fields=['included_balance'])

    first = AIWalletService.reserve(tenant, Decimal('60'), key='parallel:first')
    AIWalletService.reserve(tenant, Decimal('40'), key='parallel:second')

    charged = AIWalletService.settle(tenant, first, Decimal('80'))
    summary = AIWalletService.summary(tenant)
    assert charged == Decimal('60')
    assert summary['reserved'] == Decimal('40')
    assert summary['included'] == Decimal('40')


@pytest.mark.django_db
@override_settings(OPENAI_API_KEY='test-openai-key')
def test_owner_can_select_default_and_task_models_via_api():
    tenant, _ = make_tenant('model-picker-co')
    access_token = owner_access_token(tenant)
    models = list(AIModel.objects.filter(
        provider=AIModel.PROVIDER_OPENAI,
        is_active=True,
    ).order_by('sort_order'))
    assert len(models) >= 2

    response = Client().patch(
        '/api/v1/ai/settings/',
        {
            'default_model': models[1].pk,
            'use_task_overrides': True,
            'task_models': {
                AITaskType.DESCRIPTION: models[0].pk,
                AITaskType.CLASSIFICATION: models[1].pk,
            },
        },
        content_type='application/json',
        HTTP_AUTHORIZATION=f'Bearer {access_token}',
    )

    assert response.status_code == 200
    data = response.json()['data']
    assert data['default_model'] == models[1].pk
    assert data['use_task_overrides'] is True
    assert data['task_models'][AITaskType.DESCRIPTION] == models[0].pk
    assert TenantAITaskModel.objects.filter(
        settings__tenant=tenant,
        task_type=AITaskType.CLASSIFICATION,
        model=models[1],
    ).exists()


@pytest.mark.django_db
def test_model_catalog_exposes_unavailable_models_with_reason():
    _tenant, api_key = make_tenant('model-catalog-co')
    AIModel.objects.create(
        provider=AIModel.PROVIDER_OPENAI,
        external_id='disabled-test-model',
        display_name='Disabled test model',
        supported_tasks=[AITaskType.DESCRIPTION],
        is_active=False,
    )

    response = Client().get(
        '/api/v1/ai/models/',
        HTTP_AUTHORIZATION=f'Bearer {api_key}',
    )

    assert response.status_code == 200
    item = next(
        item
        for item in response.json()['data']
        if item['external_id'] == 'disabled-test-model'
    )
    assert item['is_selectable'] is False
    assert item['availability_reason'] == 'Модель отключена администратором.'


@pytest.mark.django_db
def test_owner_cannot_select_model_without_provider_key():
    tenant, _ = make_tenant('model-unavailable-co')
    access_token = owner_access_token(tenant)
    model = AIModel.objects.create(
        provider=AIModel.PROVIDER_DEEPSEEK,
        external_id='deepseek-unconfigured-test',
        display_name='DeepSeek unconfigured',
        supported_tasks=[AITaskType.DESCRIPTION],
        is_active=True,
        is_pricing_verified=True,
    )

    with override_settings(DEEPSEEK_API_KEY=''):
        response = Client().patch(
            '/api/v1/ai/settings/',
            {
                'default_model': model.pk,
                'use_task_overrides': False,
                'task_models': {},
            },
            content_type='application/json',
            HTTP_AUTHORIZATION=f'Bearer {access_token}',
        )

    assert response.status_code == 400
    assert response.json()['code'] == 'model_unavailable'
    assert response.json()['message'] == 'API-ключ провайдера не настроен.'


@pytest.mark.django_db
@override_settings(OPENAI_API_KEY='test-openai-key')
def test_budget_openai_models_are_seeded_and_cheaper_than_luna():
    expected_ids = {
        'gpt-5-nano',
        'gpt-5-mini',
        'gpt-5.4-nano',
        'gpt-5.4-mini',
        'gpt-5.4',
        'gpt-5.5',
    }
    models = {
        model.external_id: model
        for model in AIModel.objects.filter(external_id__in=expected_ids)
    }
    luna = AIModel.objects.get(external_id='gpt-5.6-luna')

    assert set(models) == expected_ids
    assert all(model.is_selectable for model in models.values())
    assert models['gpt-5-nano'].estimate_credits(2000, 800) < (
        models['gpt-5.4-nano'].estimate_credits(2000, 800)
    )
    assert models['gpt-5.4-nano'].estimate_credits(2000, 800) < (
        luna.estimate_credits(2000, 800)
    )
    assert models['gpt-5.5'].estimate_credits(2000, 800) > (
        luna.estimate_credits(2000, 800)
    )
