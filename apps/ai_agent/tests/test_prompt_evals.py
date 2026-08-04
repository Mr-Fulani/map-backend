from apps.ai_agent.evals import (
    compare_prompt_results,
    evaluate_description_output,
    golden_description_cases,
)


def _valid_output(case):
    data = case['product_data']
    return {
        'title': (
            f'{data["name"]} {data["brand"]} {data["article"]} '
            f'для обслуживания автомобиля, новое состояние'
        ),
        'description': (
            f'{data["name"]} {data["brand"]}, артикул {data["article"]}. '
            f'Категория: {data["category"]}. Состояние: {data["condition"]}.'
        ),
        'confidence': 0.8,
    }


def test_golden_set_contains_50_auto_part_cases():
    cases = golden_description_cases()
    assert len(cases) == 50
    assert len({case['id'] for case in cases}) == 50


def test_all_golden_reference_outputs_pass_contract():
    for case in golden_description_cases():
        assert evaluate_description_output(case, _valid_output(case))['passed'] is True


def test_comparison_detects_prompt_improvement():
    cases = golden_description_cases()
    old_outputs = {
        case['id']: {'title': 'Коротко', 'description': 'Лучший товар'}
        for case in cases
    }
    new_outputs = {case['id']: _valid_output(case) for case in cases}

    comparison = compare_prompt_results(cases, old_outputs, new_outputs)

    assert comparison['old_pass_rate'] == 0
    assert comparison['new_pass_rate'] == 1
