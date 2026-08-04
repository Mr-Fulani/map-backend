"""Deterministic offline evals; they never call a paid model during the test suite."""

from statistics import mean

from apps.ai_agent.validators import BANNED_WORDS, VAGUE_FITMENT_PHRASES


PARTS = [
    ('Тормозной диск', 'Тормозная система'),
    ('Тормозные колодки', 'Тормозная система'),
    ('Масляный фильтр', 'Фильтры'),
    ('Воздушный фильтр', 'Фильтры'),
    ('Свеча зажигания', 'Система зажигания'),
    ('Амортизатор', 'Подвеска'),
    ('Ступичный подшипник', 'Ходовая часть'),
    ('Ремень ГРМ', 'Двигатель'),
    ('Рулевая тяга', 'Рулевое управление'),
    ('Водяной насос', 'Система охлаждения'),
]
BRANDS = ['BOSCH', 'BREMBO', 'MANN-FILTER', 'SACHS', 'SKF']


def golden_description_cases() -> list[dict]:
    cases = []
    for part_index, (part_name, category) in enumerate(PARTS, start=1):
        for brand_index, brand in enumerate(BRANDS, start=1):
            article = f'{brand[:3]}-{part_index:02d}{brand_index:02d}'
            cases.append({
                'id': f'auto-part-{part_index:02d}-{brand_index:02d}',
                'product_data': {
                    'article': article,
                    'name': part_name,
                    'brand': brand,
                    'category': category,
                    'condition': 'Новое',
                    'description_1c': '',
                },
                'required_terms': [part_name, brand, article],
                'forbidden_terms': [
                    *BANNED_WORDS,
                    *VAGUE_FITMENT_PHRASES,
                    'телефон',
                    'доставка',
                ],
            })
    return cases


def evaluate_description_output(case: dict, output: dict) -> dict:
    title = str(output.get('title', '')).strip()
    description = str(output.get('description', '')).strip()
    combined = f'{title} {description}'.casefold()
    missing_terms = [
        term for term in case['required_terms']
        if str(term).casefold() not in combined
    ]
    forbidden_terms = [
        term for term in case['forbidden_terms']
        if str(term).casefold() in combined
    ]
    checks = {
        'valid_title_length': 50 <= len(title) <= 200,
        'valid_description_length': 0 < len(description) <= 7500,
        'required_terms_present': not missing_terms,
        'forbidden_terms_absent': not forbidden_terms,
    }
    return {
        'case_id': case['id'],
        'passed': all(checks.values()),
        'score': sum(checks.values()) / len(checks),
        'checks': checks,
        'missing_terms': missing_terms,
        'forbidden_terms': forbidden_terms,
    }


def compare_prompt_results(
    cases: list[dict],
    old_outputs: dict[str, dict],
    new_outputs: dict[str, dict],
) -> dict:
    old = [evaluate_description_output(case, old_outputs.get(case['id'], {})) for case in cases]
    new = [evaluate_description_output(case, new_outputs.get(case['id'], {})) for case in cases]
    return {
        'cases': len(cases),
        'old_pass_rate': mean(result['passed'] for result in old) if old else 0,
        'new_pass_rate': mean(result['passed'] for result in new) if new else 0,
        'old_average_score': mean(result['score'] for result in old) if old else 0,
        'new_average_score': mean(result['score'] for result in new) if new else 0,
    }
