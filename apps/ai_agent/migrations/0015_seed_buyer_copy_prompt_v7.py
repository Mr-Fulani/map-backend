from django.db import migrations


PROMPT_REPLACEMENTS = [
    (
        'или «является важной частью автомобиля».\n',
        'или «является важной частью автомобиля». Не дублируй WVA и другие '
        'справочные номера в первом абзаце, если ниже для них есть отдельный раздел.\n',
    ),
    (
        'groups, укажи confirmed_fitment_count и предложи сверить номер детали или VIN. '
        'Не перечисляй поколения и модификации длинной простынёй. Не утверждай, ',
        'groups, укажи confirmed_fitment_count и предложи сверить номер детали или VIN. '
        'Пиши естественно: «Подтверждена совместимость с N вариантами Mercedes-Benz: '
        'E-Class и CLS». Не называй варианты «записями» или «подтверждениями» и не '
        'показывай названия внутренних полей. Формулируй «сверьте по VIN или '
        'каталожному номеру», никогда не соединяй их косой чертой. Не перечисляй '
        'поколения и модификации длинной простынёй. Не утверждай, ',
    ),
    (
        'термины OEM/Cross и не называй номера «торговыми», если источник этого не '
        'доказал.\n',
        'термины OEM/Cross и не называй номера «торговыми», если источник этого не '
        'доказал. Если WVA и торговые номера совпадают, покажи их один раз как WVA.\n',
    ),
    (
        'или «надёжная работа», если это не подтверждённые факты.\n',
        'или «надёжная работа», если это не подтверждённые факты. Не пиши про '
        '«количество записей», «подтверждения» и «диапазоны дат»: это внутренние '
        'данные, не польза для покупателя.\n',
    ),
]


def seed_prompt_v7(apps, schema_editor):
    AIPromptTemplate = apps.get_model('ai_agent', 'AIPromptTemplate')
    scope = {
        'task_type': 'description_generation',
        'catalog_domain': 'auto_parts',
        'marketplace': 'avito',
    }
    previous = AIPromptTemplate.objects.filter(**scope, version=6).first()
    if previous is None:
        return
    system_prompt = previous.system_prompt
    for old, new in PROMPT_REPLACEMENTS:
        if old not in system_prompt:
            raise RuntimeError('Не удалось построить prompt v7 из сохранённого prompt v6.')
        system_prompt = system_prompt.replace(old, new, 1)

    AIPromptTemplate.objects.filter(**scope, is_active=True).update(is_active=False)
    prompt, _ = AIPromptTemplate.objects.get_or_create(
        **scope,
        version=7,
        defaults={
            'name': 'Avito — чистая покупательская карточка автозапчасти',
            'system_prompt': system_prompt,
            'output_schema': previous.output_schema,
            'change_notes': (
                'Убраны внутренние счётчики, дубли WVA и неестественные фразы; '
                'совместимость и проверка по VIN формулируются для покупателя.'
            ),
        },
    )
    if not prompt.is_active:
        prompt.is_active = True
        prompt.save(update_fields=['is_active'])


def unseed_prompt_v7(apps, schema_editor):
    AIPromptTemplate = apps.get_model('ai_agent', 'AIPromptTemplate')
    scope = {
        'task_type': 'description_generation',
        'catalog_domain': 'auto_parts',
        'marketplace': 'avito',
    }
    AIPromptTemplate.objects.filter(**scope, version=7).delete()
    AIPromptTemplate.objects.filter(**scope, version=6).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ('ai_agent', '0014_seed_adaptive_auto_parts_prompt_v6'),
    ]

    operations = [
        migrations.RunPython(seed_prompt_v7, unseed_prompt_v7),
    ]
