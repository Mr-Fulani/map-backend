from types import SimpleNamespace

from apps.products.attribute_presentation import (
    normalize_attribute_text,
    presented_attributes,
)


def _attribute(name, value):
    return SimpleNamespace(name=name, value=value)


def test_normalizes_bad_caliper_translation_and_accessory_contradiction():
    name, value = normalize_attribute_text(
        'Комплектность',
        'Без аксессуаров, с винтами тормозных сателлитов, с прижимной пластиной',
    )

    assert name == 'Комплектность'
    assert value == 'с болтами тормозного суппорта, с противоскрипной пластиной'


def test_hides_trade_numbers_when_they_duplicate_wva():
    attributes = [
        _attribute('WVA номер', '22437, 22438'),
        _attribute('Торговые номера', '22437, 22438'),
    ]

    result = presented_attributes(attributes)

    assert [(name, value) for _item, name, value in result] == [
        ('WVA', '22437, 22438'),
    ]


def test_barcode_is_kept_for_ui_but_omitted_from_ai_copy():
    attributes = [_attribute('Номер EAN/Штрих-код', '8020584086988')]

    assert len(presented_attributes(attributes)) == 1
    assert presented_attributes(attributes, for_ai=True) == []
