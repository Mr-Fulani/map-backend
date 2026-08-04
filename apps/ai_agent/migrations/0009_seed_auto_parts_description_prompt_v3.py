from django.db import migrations


OLD_DESCRIPTION_RULE = (
    '4. Описание до 7500 символов. Первый абзац кратко объясняет, что это за деталь. '
    'Затем при наличии данных: применяемость, характеристики, OEM/Cross-коды, состояние.\n'
)

NEW_DESCRIPTION_RULE = (
    '4. Описание до 7500 символов. Первый абзац кратко объясняет, что это за деталь. '
    'Если trusted_fitments не пуст, обязательно добавь отдельный раздел '
    '«Подходит к автомобилям» и перечисли каждую подтверждённую пару make + model. '
    'Для каждой пары сохрани переданные generation, годы, modification, engine_code и '
    'power_hp, если они есть. Не скрывай применяемость общей фразой и не заменяй список '
    'одним примером. Затем укажи характеристики, OEM/Cross-коды и состояние.\n'
)


def seed_prompt_v3(apps, schema_editor):
    AIPromptTemplate = apps.get_model('ai_agent', 'AIPromptTemplate')
    scope = {
        'task_type': 'description_generation',
        'catalog_domain': 'auto_parts',
        'marketplace': 'avito',
    }
    previous = AIPromptTemplate.objects.filter(**scope, version=2).first()
    if previous is None:
        return
    system_prompt = previous.system_prompt.replace(
        OLD_DESCRIPTION_RULE,
        NEW_DESCRIPTION_RULE,
    )
    AIPromptTemplate.objects.filter(**scope, is_active=True).update(is_active=False)
    prompt, _ = AIPromptTemplate.objects.get_or_create(
        **scope,
        version=3,
        defaults={
            'name': 'Avito — автозапчасти с обязательной применяемостью',
            'system_prompt': system_prompt,
            'output_schema': previous.output_schema,
            'change_notes': (
                'Структурированная применяемость и обязательное перечисление всех '
                'подтверждённых марок и моделей в описании.'
            ),
        },
    )
    if not prompt.is_active:
        prompt.is_active = True
        prompt.save(update_fields=['is_active'])


def unseed_prompt_v3(apps, schema_editor):
    AIPromptTemplate = apps.get_model('ai_agent', 'AIPromptTemplate')
    scope = {
        'task_type': 'description_generation',
        'catalog_domain': 'auto_parts',
        'marketplace': 'avito',
    }
    AIPromptTemplate.objects.filter(**scope, version=3).delete()
    AIPromptTemplate.objects.filter(**scope, version=2).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ('ai_agent', '0008_seed_versioned_description_prompts'),
    ]

    operations = [
        migrations.RunPython(seed_prompt_v3, unseed_prompt_v3),
    ]
