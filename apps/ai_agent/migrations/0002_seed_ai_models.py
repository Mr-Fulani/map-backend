from django.db import migrations


TASKS = [
    'description_generation',
    'classification',
    'attribute_extraction',
    'fitment_resolution',
]


def seed_models(apps, schema_editor):
    AIModel = apps.get_model('ai_agent', 'AIModel')
    TenantAISettings = apps.get_model('ai_agent', 'TenantAISettings')
    Tenant = apps.get_model('tenants', 'Tenant')

    models = [
        {
            'external_id': 'gpt-5.6-luna',
            'provider': 'openai',
            'display_name': 'GPT-5.6 Luna',
            'description': 'Быстрая модель для массовых повседневных задач.',
            'quality_tier': 'standard',
            'speed_tier': 'fast',
            'input_credits_per_million': '200',
            'cached_input_credits_per_million': '20',
            'output_credits_per_million': '1200',
            'minimum_credits': '1',
            'reasoning_effort': 'low',
            'is_active': True,
            'is_default': True,
            'is_fallback': True,
            'sort_order': 10,
        },
        {
            'external_id': 'gpt-5.6-terra',
            'provider': 'openai',
            'display_name': 'GPT-5.6 Terra',
            'description': 'Баланс качества и стоимости для сложных товарных данных.',
            'quality_tier': 'advanced',
            'speed_tier': 'balanced',
            'input_credits_per_million': '500',
            'cached_input_credits_per_million': '50',
            'output_credits_per_million': '3000',
            'minimum_credits': '2',
            'reasoning_effort': 'low',
            'is_active': True,
            'is_default': False,
            'is_fallback': True,
            'sort_order': 20,
        },
        {
            'external_id': 'gpt-5.6-sol',
            'provider': 'openai',
            'display_name': 'GPT-5.6 Sol',
            'description': 'Максимальное качество для сложных и ответственных задач.',
            'quality_tier': 'maximum',
            'speed_tier': 'slow',
            'input_credits_per_million': '1000',
            'cached_input_credits_per_million': '100',
            'output_credits_per_million': '6000',
            'minimum_credits': '4',
            'reasoning_effort': 'medium',
            'is_active': True,
            'is_default': False,
            'is_fallback': False,
            'sort_order': 30,
        },
        {
            'external_id': 'claude-haiku-4-5-20251001',
            'provider': 'anthropic',
            'display_name': 'Claude Haiku 4.5',
            'description': 'Быстрая модель Anthropic для массовых задач.',
            'quality_tier': 'standard',
            'speed_tier': 'fast',
            'input_credits_per_million': '200',
            'cached_input_credits_per_million': '20',
            'output_credits_per_million': '1000',
            'minimum_credits': '1',
            'reasoning_effort': '',
            'is_active': False,
            'is_default': False,
            'is_fallback': True,
            'sort_order': 40,
        },
        {
            'external_id': 'claude-sonnet-5',
            'provider': 'anthropic',
            'display_name': 'Claude Sonnet 5',
            'description': 'Сильная модель Anthropic для сложной генерации.',
            'quality_tier': 'advanced',
            'speed_tier': 'balanced',
            'input_credits_per_million': '400',
            'cached_input_credits_per_million': '40',
            'output_credits_per_million': '2000',
            'minimum_credits': '2',
            'reasoning_effort': '',
            'is_active': False,
            'is_default': False,
            'is_fallback': True,
            'sort_order': 50,
        },
        {
            'external_id': 'claude-opus-5',
            'provider': 'anthropic',
            'display_name': 'Claude Opus 5',
            'description': 'Максимальное качество Anthropic.',
            'quality_tier': 'maximum',
            'speed_tier': 'slow',
            'input_credits_per_million': '1000',
            'cached_input_credits_per_million': '100',
            'output_credits_per_million': '5000',
            'minimum_credits': '4',
            'reasoning_effort': '',
            'is_active': False,
            'is_default': False,
            'is_fallback': False,
            'sort_order': 60,
        },
    ]
    default_model = None
    for item in models:
        external_id = item.pop('external_id')
        model, _ = AIModel.objects.update_or_create(
            external_id=external_id,
            defaults={**item, 'supported_tasks': TASKS},
        )
        if model.is_default:
            default_model = model

    if default_model:
        for tenant in Tenant.objects.all().iterator():
            TenantAISettings.objects.get_or_create(
                tenant=tenant,
                defaults={'default_model': default_model},
            )


def unseed_models(apps, schema_editor):
    AIModel = apps.get_model('ai_agent', 'AIModel')
    AIModel.objects.filter(
        external_id__in=[
            'gpt-5.6-luna',
            'gpt-5.6-terra',
            'gpt-5.6-sol',
            'claude-haiku-4-5-20251001',
            'claude-sonnet-5',
            'claude-opus-5',
        ],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ai_agent', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(seed_models, unseed_models),
    ]
