from django.db import migrations


OLD_RULE_END = 'одним примером. Затем укажи характеристики, OEM/Cross-коды и состояние.\n'
NEW_RULE_END = (
    'одним примером. Затем укажи характеристики, OEM/Cross-коды и состояние. '
    'Копируй OEM/Cross-коды как неделимые строки: не удаляй и не меняй символы.\n'
)


def seed_prompt_v4(apps, schema_editor):
    AIPromptTemplate = apps.get_model('ai_agent', 'AIPromptTemplate')
    scope = {
        'task_type': 'description_generation',
        'catalog_domain': 'auto_parts',
        'marketplace': 'avito',
    }
    previous = AIPromptTemplate.objects.filter(**scope, version=3).first()
    if previous is None:
        return
    system_prompt = previous.system_prompt.replace(OLD_RULE_END, NEW_RULE_END)
    AIPromptTemplate.objects.filter(**scope, is_active=True).update(is_active=False)
    prompt, _ = AIPromptTemplate.objects.get_or_create(
        **scope,
        version=4,
        defaults={
            'name': 'Avito — автозапчасти с точными OEM-кодами',
            'system_prompt': system_prompt,
            'output_schema': previous.output_schema,
            'change_notes': (
                'OEM/Cross-коды копируются без удаления и изменения символов; '
                'сервер дополнительно защищает подтверждённые идентификаторы.'
            ),
        },
    )
    if not prompt.is_active:
        prompt.is_active = True
        prompt.save(update_fields=['is_active'])


def unseed_prompt_v4(apps, schema_editor):
    AIPromptTemplate = apps.get_model('ai_agent', 'AIPromptTemplate')
    scope = {
        'task_type': 'description_generation',
        'catalog_domain': 'auto_parts',
        'marketplace': 'avito',
    }
    AIPromptTemplate.objects.filter(**scope, version=4).delete()
    AIPromptTemplate.objects.filter(**scope, version=3).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ('ai_agent', '0011_seed_web_research_task'),
    ]

    operations = [
        migrations.RunPython(seed_prompt_v4, unseed_prompt_v4),
    ]
