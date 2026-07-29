from django.db import migrations


TASKS = [
    'description_generation',
    'classification',
    'attribute_extraction',
    'fitment_resolution',
]


MODELS = [
    {
        'external_id': 'gpt-5-nano',
        'display_name': 'GPT-5 Nano',
        'description': 'Самая экономичная OpenAI-модель для классификации и простых массовых задач.',
        'quality_tier': 'standard',
        'speed_tier': 'fast',
        'input_credits_per_million': '10',
        'cached_input_credits_per_million': '1',
        'output_credits_per_million': '80',
        'minimum_credits': '0.1',
        'reasoning_effort': 'low',
        'sort_order': 1,
    },
    {
        'external_id': 'gpt-5.4-nano',
        'display_name': 'GPT-5.4 Nano',
        'description': 'Недорогая модель для классификации, извлечения данных и ранжирования.',
        'quality_tier': 'standard',
        'speed_tier': 'fast',
        'input_credits_per_million': '40',
        'cached_input_credits_per_million': '4',
        'output_credits_per_million': '250',
        'minimum_credits': '0.25',
        'reasoning_effort': 'low',
        'sort_order': 2,
    },
    {
        'external_id': 'gpt-5-mini',
        'display_name': 'GPT-5 Mini',
        'description': 'Экономичная универсальная модель для хорошо определённых задач.',
        'quality_tier': 'standard',
        'speed_tier': 'fast',
        'input_credits_per_million': '50',
        'cached_input_credits_per_million': '5',
        'output_credits_per_million': '400',
        'minimum_credits': '0.25',
        'reasoning_effort': 'low',
        'sort_order': 3,
    },
    {
        'external_id': 'gpt-5.4-mini',
        'display_name': 'GPT-5.4 Mini',
        'description': 'Баланс качества, скорости и цены для массовой генерации.',
        'quality_tier': 'advanced',
        'speed_tier': 'fast',
        'input_credits_per_million': '150',
        'cached_input_credits_per_million': '15',
        'output_credits_per_million': '900',
        'minimum_credits': '0.5',
        'reasoning_effort': 'low',
        'sort_order': 4,
    },
    {
        'external_id': 'gpt-5.4',
        'display_name': 'GPT-5.4',
        'description': 'Предыдущее поколение сильной модели OpenAI для сложных задач.',
        'quality_tier': 'advanced',
        'speed_tier': 'balanced',
        'input_credits_per_million': '500',
        'cached_input_credits_per_million': '50',
        'output_credits_per_million': '3000',
        'minimum_credits': '2',
        'reasoning_effort': 'low',
        'sort_order': 25,
    },
    {
        'external_id': 'gpt-5.5',
        'display_name': 'GPT-5.5',
        'description': 'Предыдущее frontier-поколение OpenAI; по цене сопоставимо с GPT-5.6 Sol.',
        'quality_tier': 'maximum',
        'speed_tier': 'slow',
        'input_credits_per_million': '1000',
        'cached_input_credits_per_million': '100',
        'output_credits_per_million': '6000',
        'minimum_credits': '4',
        'reasoning_effort': 'medium',
        'sort_order': 35,
    },
]


def seed_models(apps, schema_editor):
    AIModel = apps.get_model('ai_agent', 'AIModel')
    for item in MODELS:
        defaults = {
            'provider': 'openai',
            'supported_tasks': TASKS,
            'max_output_tokens': 2048,
            'is_active': True,
            'is_pricing_verified': True,
            'is_default': False,
            'is_fallback': False,
            **item,
        }
        external_id = defaults.pop('external_id')
        AIModel.objects.update_or_create(
            external_id=external_id,
            defaults=defaults,
        )


def unseed_models(apps, schema_editor):
    AIModel = apps.get_model('ai_agent', 'AIModel')
    AIModel.objects.filter(
        external_id__in=[item['external_id'] for item in MODELS],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('ai_agent', '0004_seed_multi_provider_models'),
    ]

    operations = [
        migrations.RunPython(seed_models, unseed_models),
    ]
