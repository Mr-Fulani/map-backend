from pathlib import Path

from django.template.loader import get_template
from django.utils.translation import override


def test_local_admin_theme_switch_uses_bounded_three_column_layout():
    template = get_template('unfold/helpers/theme_switch.html')
    expected = Path(__file__).resolve().parents[1] / (
        'templates/unfold/helpers/theme_switch.html'
    )

    assert Path(template.origin.name).resolve() == expected.resolve()

    source = expected.read_text(encoding='utf-8')
    assert 'data-testid="admin-theme-switch"' in source
    assert 'grid grid-cols-3' in source
    assert source.count('min-w-0') == 3
    assert source.count('max-w-full') == 3
    assert source.count('aria-label=') == 3
    assert source.count('x-bind:aria-pressed=') == 3


def test_local_admin_theme_switch_renders_all_russian_labels():
    template = get_template('unfold/helpers/theme_switch.html')

    with override('ru'):
        rendered = template.render({})

    assert 'Светлая' in rendered
    assert 'Тёмная' in rendered
    assert 'Системная' in rendered
    assert rendered.count('role="button"') == 3
