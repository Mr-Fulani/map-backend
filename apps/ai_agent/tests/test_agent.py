import json
from unittest.mock import MagicMock, patch

import anthropic
import pytest

from apps.ai_agent.services import AICreditsExhausted, DescriptionAgent
from apps.ai_agent.validators import (
    BannedWordsError,
    VagueFitmentError,
    ValidationError,
    strip_contacts,
    validate_description,
    validate_json_response,
    validate_title,
)
from apps.datasources.encryption import encrypt
from apps.datasources.models import DataSourceConnection
from apps.products.models import ProductCrossCode
from apps.products.services import ProductService
from apps.products.services import ProductEnrichmentService
from apps.tenants.services import TenantService


def make_tenant(slug):
    tenant, _ = TenantService.create_tenant(slug, slug, f'{slug}@test.com', 'pass12345')
    return tenant


def make_product(tenant):
    ds = DataSourceConnection.objects.create(
        tenant=tenant, name='S', type='1c_http',
        credentials=encrypt({'url': 'http://x.com', 'user': 'u', 'password': 'p'}),
    )
    product, _ = ProductService.upsert_from_source(tenant, ds, {
        'uuid': None, 'article': 'ART-001', 'name': 'Тормозной диск передний',
        'brand': 'Bosch', 'price': '3500', 'stock_qty': 5,
        'category': 'Тормозная система', 'condition': 'new',
    })
    return product


VALID_RESPONSE = json.dumps({
    'title': 'Тормозной диск Bosch передний для Toyota Camry',
    'description': 'Тормозной диск производства Bosch. Подходит для Toyota Camry V40 2006-2011. '
                   'Диаметр 296 мм, вентилируемый. Состояние: новый, оригинальная упаковка.',
    'confidence': 0.87,
})


def _mock_claude_response(text: str):
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


class TestValidators:
    def test_validate_title_ok(self):
        assert validate_title('Тормозной диск Bosch для Toyota') == 'Тормозной диск Bosch для Toyota'

    def test_validate_title_too_short(self):
        with pytest.raises(ValidationError):
            validate_title('Диск')

    def test_validate_title_too_long(self):
        with pytest.raises(ValidationError):
            validate_title('А' * 101)

    def test_validate_description_truncates_at_7500(self):
        long_text = 'Слово ' * 2000
        result = validate_description(long_text)
        assert len(result) <= 7500

    def test_banned_words_raise_error(self):
        with pytest.raises(BannedWordsError):
            validate_description('Лучший товар на рынке!')

    def test_vague_fitment_phrase_raises_error(self):
        with pytest.raises(VagueFitmentError):
            validate_description(
                'Подходят для различных моделей автомобилей, обеспечивая надежную остановку.'
            )
        with pytest.raises(VagueFitmentError):
            validate_description('Подходит для некоторых моделей автомобилей.')

    def test_strip_contacts_removes_phone(self):
        text = 'Звоните +7 (999) 123-45-67 для уточнения'
        result = strip_contacts(text)
        assert '+7' not in result

    def test_strip_contacts_keeps_part_number(self):
        text = 'Артикул: 56500-D4800'
        assert strip_contacts(text) == text

    def test_strip_contacts_removes_email(self):
        text = 'Пишите на test@example.com'
        result = strip_contacts(text)
        assert '@' not in result

    def test_strip_contacts_removes_url(self):
        text = 'Смотрите на https://shop.ru/item'
        result = strip_contacts(text)
        assert 'https://' not in result

    def test_validate_json_strips_markdown_backticks(self):
        raw = f'```json\n{VALID_RESPONSE}\n```'
        data = validate_json_response(raw)
        assert 'title' in data

    def test_validate_json_invalid_raises(self):
        with pytest.raises(ValidationError):
            validate_json_response('not json at all')


@pytest.mark.django_db
class TestDescriptionAgent:
    def test_generate_returns_valid_structure(self):
        tenant = make_tenant('gen-co')
        product = make_product(tenant)

        with patch('apps.ai_agent.services.anthropic.Anthropic') as mock_cls:
            mock_cls.return_value.messages.create.return_value = _mock_claude_response(VALID_RESPONSE)
            result = DescriptionAgent().generate(product, tenant)

        assert 'title' in result
        assert 'description' in result
        assert 'confidence' in result
        assert 20 <= len(result['title']) <= 100
        assert len(result['description']) <= 7500

    def test_ai_credits_incremented_atomically(self):
        tenant = make_tenant('credits-co')
        product = make_product(tenant)
        initial = tenant.ai_credits_used

        with patch('apps.ai_agent.services.anthropic.Anthropic') as mock_cls:
            mock_cls.return_value.messages.create.return_value = _mock_claude_response(VALID_RESPONSE)
            DescriptionAgent().generate(product, tenant)

        tenant.refresh_from_db()
        assert tenant.ai_credits_used == initial + 1

    def test_banned_words_trigger_retry(self):
        tenant = make_tenant('banned-co')
        product = make_product(tenant)

        banned_response = json.dumps({
            'title': 'Тормозной диск — лучший выбор для вашего авто',
            'description': 'Это лучший тормозной диск на рынке.',
            'confidence': 0.9,
        })

        with patch('apps.ai_agent.services.anthropic.Anthropic') as mock_claude, \
             patch('apps.ai_agent.services.DescriptionAgent._call_openai') as mock_openai:
            mock_claude.return_value.messages.create.return_value = _mock_claude_response(banned_response)
            mock_openai.return_value = json.loads(VALID_RESPONSE)
            result = DescriptionAgent().generate(product, tenant)

        assert result['title'] == json.loads(VALID_RESPONSE)['title']
        mock_openai.assert_called_once()

    def test_vague_fitment_triggers_retry(self):
        tenant = make_tenant('vague-fitment-co')
        product = make_product(tenant)

        vague_response = json.dumps({
            'title': 'Тормозной диск Bosch передний ART-001',
            'description': 'Подходит для различных моделей автомобилей. Состояние: новый.',
            'confidence': 0.7,
        })

        with patch('apps.ai_agent.services.anthropic.Anthropic') as mock_claude, \
             patch('apps.ai_agent.services.DescriptionAgent._call_openai') as mock_openai:
            mock_claude.return_value.messages.create.return_value = _mock_claude_response(vague_response)
            mock_openai.return_value = json.loads(VALID_RESPONSE)
            result = DescriptionAgent().generate(product, tenant)

        assert result['title'] == json.loads(VALID_RESPONSE)['title']
        mock_openai.assert_called_once()

    def test_fallback_to_openai_when_claude_fails(self):
        tenant = make_tenant('fallback-co')
        product = make_product(tenant)

        with patch('apps.ai_agent.services.anthropic.Anthropic') as mock_claude, \
             patch('apps.ai_agent.services.DescriptionAgent._call_openai') as mock_openai:
            mock_claude.return_value.messages.create.side_effect = anthropic.APIConnectionError(
                request=MagicMock()
            )
            mock_openai.return_value = json.loads(VALID_RESPONSE)
            result = DescriptionAgent().generate(product, tenant)

        assert result['confidence'] == 0.87
        mock_openai.assert_called_once()

    def test_credits_exhausted_raises(self):
        tenant = make_tenant('exhausted-co')
        product = make_product(tenant)

        with patch('apps.ai_agent.services.LimitChecker.can_generate_ai', return_value=(False, 'лимит')):
            with pytest.raises(AICreditsExhausted):
                DescriptionAgent().generate(product, tenant)

    def test_build_message_includes_enrichment_data(self):
        tenant = make_tenant('enriched-agent-co')
        product = make_product(tenant)
        ProductEnrichmentService.create_attribute(
            tenant=tenant, product=product, name='Ширина', value='114 мм',
        )
        ProductEnrichmentService.create_cross_code(
            tenant=tenant, product=product, manufacturer='MERCEDES-BENZ',
            code='A0004206000', normalized_code='A0004206000',
            code_type=ProductCrossCode.CodeType.OEM,
        )
        ProductEnrichmentService.create_fitment(
            tenant=tenant, product=product, make='MERCEDES-BENZ',
            model='E-CLASS', generation='W213', modification='E 220 d',
            power_hp=194,
        )

        message = DescriptionAgent()._build_message(product)

        assert 'Проверенные данные обогащения' in message
        assert 'Ширина: 114 мм' in message
        assert 'Вероятные марки авто по OEM/Cross: MERCEDES-BENZ' in message
        assert 'MERCEDES-BENZ: A0004206000' in message
        assert 'MERCEDES-BENZ E-CLASS W213 E 220 d 194 л.с.' in message

    def test_build_message_uses_vehicle_make_from_cross_without_fitment(self):
        tenant = make_tenant('cross-make-agent-co')
        product = make_product(tenant)
        ProductEnrichmentService.create_cross_code(
            tenant=tenant, product=product, manufacturer='HYUNDAI / KIA',
            code='56500D4800', normalized_code='56500D4800',
            code_type=ProductCrossCode.CodeType.OEM,
        )

        message = DescriptionAgent()._build_message(product)

        assert 'Вероятные марки авто по OEM/Cross: HYUNDAI, KIA' in message

    def test_build_message_excludes_reviewable_fitments(self):
        tenant = make_tenant('reviewable-fitment-agent-co')
        product = make_product(tenant)
        ProductEnrichmentService.create_fitment(
            tenant=tenant, product=product, make='MERCEDES-BENZ',
            model='E-CLASS', generation='W213', confidence=0.95,
        )
        ProductEnrichmentService.create_fitment(
            tenant=tenant, product=product, make='BMW',
            model='5', generation='G30', confidence=0.4, needs_review=True,
        )

        message = DescriptionAgent()._build_message(product)

        assert 'MERCEDES-BENZ E-CLASS W213' in message
        assert 'BMW 5 G30' not in message

    def test_no_regeneration_on_price_change(self):
        """price_only изменение не требует перегенерации (через detect_change_type)."""
        from apps.products.services import ProductService
        old = {'price': '100', 'stock_qty': 5, 'name': 'X', 'brand': 'Y',
               'condition': 'new', 'category': 'A'}
        new = {'price': '200', 'stock_qty': 5, 'name': 'X', 'brand': 'Y',
               'condition': 'new', 'category': 'A'}
        change_type = ProductService.detect_change_type(old, new)
        assert change_type == 'price_only'
