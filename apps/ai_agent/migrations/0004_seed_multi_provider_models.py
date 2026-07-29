from django.db import migrations


TASKS = [
    'description_generation',
    'classification',
    'attribute_extraction',
    'fitment_resolution',
]


MODELS = [
    {
        'external_id': 'gemini-3.5-flash-lite',
        'provider': 'gemini',
        'display_name': 'Gemini 3.5 Flash-Lite',
        'description': 'Экономичная модель Google для массовых быстрых задач.',
        'quality_tier': 'standard',
        'speed_tier': 'fast',
        'input_credits_per_million': '60',
        'cached_input_credits_per_million': '6',
        'output_credits_per_million': '500',
        'minimum_credits': '1',
        'sort_order': 70,
    },
    {
        'external_id': 'gemini-3.6-flash',
        'provider': 'gemini',
        'display_name': 'Gemini 3.6 Flash',
        'description': 'Быстрая мультимодальная модель Google с повышенным качеством.',
        'quality_tier': 'advanced',
        'speed_tier': 'fast',
        'input_credits_per_million': '300',
        'cached_input_credits_per_million': '30',
        'output_credits_per_million': '1500',
        'minimum_credits': '2',
        'sort_order': 80,
    },
    {
        'external_id': 'deepseek-v4-flash',
        'provider': 'deepseek',
        'display_name': 'DeepSeek V4 Flash',
        'description': 'Недорогая быстрая модель DeepSeek для регулярных задач.',
        'quality_tier': 'standard',
        'speed_tier': 'fast',
        'input_credits_per_million': '28',
        'cached_input_credits_per_million': '0.56',
        'output_credits_per_million': '56',
        'minimum_credits': '1',
        'sort_order': 90,
    },
    {
        'external_id': 'deepseek-v4-pro',
        'provider': 'deepseek',
        'display_name': 'DeepSeek V4 Pro',
        'description': 'Усиленная модель DeepSeek для задач, требующих рассуждения.',
        'quality_tier': 'advanced',
        'speed_tier': 'balanced',
        'input_credits_per_million': '87',
        'cached_input_credits_per_million': '0.725',
        'output_credits_per_million': '174',
        'minimum_credits': '1',
        'sort_order': 100,
    },
    {
        'external_id': 'kimi-k3',
        'provider': 'kimi',
        'display_name': 'Kimi K3',
        'description': 'Флагманская модель Kimi; подключение ожидает подтверждения тарифа.',
        'quality_tier': 'maximum',
        'speed_tier': 'balanced',
        'input_credits_per_million': '0',
        'cached_input_credits_per_million': '0',
        'output_credits_per_million': '0',
        'minimum_credits': '0',
        'is_pricing_verified': False,
        'sort_order': 110,
    },
    {
        'external_id': 'kimi-k2.6',
        'provider': 'kimi',
        'display_name': 'Kimi K2.6',
        'description': 'Универсальная модель Kimi; подключение ожидает подтверждения тарифа.',
        'quality_tier': 'advanced',
        'speed_tier': 'balanced',
        'input_credits_per_million': '0',
        'cached_input_credits_per_million': '0',
        'output_credits_per_million': '0',
        'minimum_credits': '0',
        'is_pricing_verified': False,
        'sort_order': 120,
    },
]


def seed_models(apps, schema_editor):
    AIModel = apps.get_model('ai_agent', 'AIModel')
    for item in MODELS:
        defaults = {
            'supported_tasks': TASKS,
            'max_output_tokens': 2048,
            'reasoning_effort': '',
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
        ('ai_agent', '0003_aimodel_is_pricing_verified_alter_aimodel_provider'),
    ]

    operations = [
        migrations.RunPython(seed_models, unseed_models),
    ]
