from django.db import migrations


OLD_RULE = (
    "8. Если trusted_fitments пуст, но есть cautious_vehicle_makes, разрешено назвать "
    "только марки и обязательно написать, что совместимость нужно сверить по номеру "
    "детали или VIN.\n"
)

NEW_RULE = (
    "8. Если trusted_fitments пуст, но product_data.name явно перечисляет марку и "
    "несколько моделей автомобилей как применяемость детали, разрешено перенести этот "
    "список без домыслов в отдельную строку «Совместимость с автомобилями: …». Сохраняй "
    "полные названия моделей. Если последнее название в исходнике уже оборвано до "
    "фрагмента вроде «VER», не додумывай его и не включай этот фрагмент в список. Не пиши "
    "метафразы «в названии позиции указаны модели», «название товара содержит» или "
    "подобные пояснения о структуре исходных данных. Обычное упоминание одного автомобиля "
    "в названии само по себе не является списком совместимости. Если есть только "
    "cautious_vehicle_makes, разрешено назвать только марки и обязательно написать, что "
    "совместимость нужно сверить по номеру детали или VIN.\n"
)


def seed_prompt_v8(apps, schema_editor):
    AIPromptTemplate = apps.get_model('ai_agent', 'AIPromptTemplate')
    scope = {
        'task_type': 'description_generation',
        'catalog_domain': 'auto_parts',
        'marketplace': 'avito',
    }
    previous = AIPromptTemplate.objects.filter(**scope, version=7).first()
    if previous is None:
        return
    if OLD_RULE not in previous.system_prompt:
        raise RuntimeError('Не удалось построить prompt v8 из сохранённого prompt v7.')
    system_prompt = previous.system_prompt.replace(OLD_RULE, NEW_RULE, 1)

    AIPromptTemplate.objects.filter(**scope, is_active=True).update(is_active=False)
    prompt, _ = AIPromptTemplate.objects.get_or_create(
        **scope,
        version=8,
        defaults={
            'name': 'Avito — совместимость из явного списка моделей',
            'system_prompt': system_prompt,
            'output_schema': previous.output_schema,
            'change_notes': (
                'Явные списки моделей из названия оформляются как совместимость; '
                'запрещены метафразы и обрезанные названия моделей.'
            ),
        },
    )
    if not prompt.is_active:
        prompt.is_active = True
        prompt.save(update_fields=['is_active'])


def unseed_prompt_v8(apps, schema_editor):
    AIPromptTemplate = apps.get_model('ai_agent', 'AIPromptTemplate')
    scope = {
        'task_type': 'description_generation',
        'catalog_domain': 'auto_parts',
        'marketplace': 'avito',
    }
    AIPromptTemplate.objects.filter(**scope, version=8).delete()
    AIPromptTemplate.objects.filter(**scope, version=7).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ('ai_agent', '0015_seed_buyer_copy_prompt_v7'),
    ]

    operations = [
        migrations.RunPython(seed_prompt_v8, unseed_prompt_v8),
    ]
