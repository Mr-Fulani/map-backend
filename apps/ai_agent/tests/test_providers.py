from unittest.mock import Mock, patch

import pytest
from django.test import override_settings

from apps.ai_agent.models import AIModel
from apps.ai_agent.providers import AIProviderError, call_model


@pytest.mark.parametrize(
    ('provider', 'setting_name', 'expected_url'),
    [
        (
            AIModel.PROVIDER_GEMINI,
            'GEMINI_API_KEY',
            'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
        ),
        (
            AIModel.PROVIDER_DEEPSEEK,
            'DEEPSEEK_API_KEY',
            'https://api.deepseek.com/chat/completions',
        ),
        (
            AIModel.PROVIDER_KIMI,
            'MOONSHOT_API_KEY',
            'https://api.moonshot.ai/v1/chat/completions',
        ),
    ],
)
def test_openai_compatible_providers_use_registry_endpoint(
    provider,
    setting_name,
    expected_url,
):
    model = AIModel(
        provider=provider,
        external_id='provider-test-model',
        display_name='Provider test',
        max_output_tokens=321,
    )
    response = Mock(status_code=200)
    response.json.return_value = {
        'model': 'provider-response-model',
        'choices': [{'message': {'content': 'Готовый ответ'}}],
        'usage': {
            'prompt_tokens': 120,
            'completion_tokens': 40,
            'prompt_tokens_details': {'cached_tokens': 20},
        },
    }

    with override_settings(**{setting_name: 'provider-test-key'}):
        with patch('apps.ai_agent.providers.requests.post', return_value=response) as post:
            result = call_model(model, 'Системная инструкция', 'Запрос')

    assert post.call_args.args[0] == expected_url
    assert post.call_args.kwargs['headers']['Authorization'] == 'Bearer provider-test-key'
    assert post.call_args.kwargs['json']['model'] == 'provider-test-model'
    assert result.text == 'Готовый ответ'
    assert result.input_tokens == 120
    assert result.cached_input_tokens == 20
    assert result.output_tokens == 40


def test_provider_without_key_is_rejected_before_request():
    model = AIModel(
        provider=AIModel.PROVIDER_DEEPSEEK,
        external_id='deepseek-v4-flash',
        display_name='DeepSeek V4 Flash',
    )

    with override_settings(DEEPSEEK_API_KEY=''):
        with patch('apps.ai_agent.providers.requests.post') as post:
            with pytest.raises(AIProviderError) as error:
                call_model(model, 'system', 'user')

    assert error.value.code == 'missing_api_key'
    post.assert_not_called()


def test_openai_responses_uses_json_schema_when_provided():
    schema = {
        'type': 'object',
        'properties': {'title': {'type': 'string'}},
        'required': ['title'],
        'additionalProperties': False,
    }
    model = AIModel(
        provider=AIModel.PROVIDER_OPENAI,
        external_id='gpt-test',
        display_name='GPT test',
    )
    response = Mock(status_code=200)
    response.json.return_value = {
        'model': 'gpt-test',
        'output_text': '{"title":"ok"}',
        'usage': {'input_tokens': 10, 'output_tokens': 5},
    }

    with override_settings(OPENAI_API_KEY='key'):
        with patch('apps.ai_agent.providers.requests.post', return_value=response) as post:
            call_model(model, 'system', 'user', output_schema=schema)

    assert post.call_args.kwargs['json']['text']['format']['schema'] == schema
