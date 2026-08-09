from dataclasses import dataclass

import requests
from django.conf import settings

from apps.core.http_responses import (
    TrustedResponseError, bounded_http_request, trusted_api_max_bytes,
)
from apps.ai_agent.models import AIModel
from apps.ai_agent.provider_registry import ProviderDefinition, get_provider


class AIProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = 'provider_error', retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


_MAX_AI_RESPONSE_ITEMS = 256


@dataclass(frozen=True)
class AIProviderResult:
    text: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    response_model: str


def call_model(
    model: AIModel,
    system_prompt: str,
    user_message: str,
    *,
    output_schema: dict | None = None,
) -> AIProviderResult:
    provider = get_provider(model.provider)
    if provider is None:
        raise AIProviderError(
            f'Неизвестный AI-провайдер: {model.provider}',
            code='unknown_provider',
        )
    if not provider.api_key:
        raise AIProviderError(
            f'{provider.api_key_setting} не настроен.',
            code='missing_api_key',
        )
    if provider.api_style == 'openai_responses':
        return _call_openai(
            model, system_prompt, user_message, provider, output_schema=output_schema,
        )
    if provider.api_style == 'anthropic_messages':
        return _call_anthropic(model, system_prompt, user_message, provider)
    if provider.api_style == 'openai_chat':
        return _call_openai_compatible_chat(
            model, system_prompt, user_message, provider,
        )
    raise AIProviderError(
        f'Неподдерживаемый формат API: {provider.api_style}',
        code='unknown_provider_api',
    )


def _call_openai(
    model: AIModel,
    system_prompt: str,
    user_message: str,
    provider: ProviderDefinition,
    *,
    output_schema: dict | None = None,
) -> AIProviderResult:
    payload = {
        'model': model.external_id,
        'instructions': system_prompt,
        'input': user_message,
        'max_output_tokens': model.max_output_tokens,
        'store': False,
    }
    if model.reasoning_effort:
        payload['reasoning'] = {'effort': model.reasoning_effort}
    if output_schema:
        payload['text'] = {
            'format': {
                'type': 'json_schema',
                'name': 'map_product_description',
                'schema': output_schema,
                'strict': True,
            },
        }
    try:
        response = bounded_http_request(
            requests.post,
            f'{provider.base_url}/responses',
            headers={
                'Authorization': f'Bearer {provider.api_key}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=120,
            max_bytes=trusted_api_max_bytes(settings),
        )
    except TrustedResponseError as exc:
        raise AIProviderError(
            f'OpenAI вернул небезопасный ответ: {exc}',
            code='invalid_provider_response',
            retryable=False,
        ) from exc
    except requests.RequestException as exc:
        raise AIProviderError(
            f'Ошибка соединения с OpenAI: {exc}',
            code='connection_error',
            retryable=True,
        ) from exc
    data = _checked_json(response, 'OpenAI')
    output_text = data.get('output_text')
    if output_text is not None and not isinstance(output_text, str):
        _invalid_provider_response('OpenAI', 'output_text must be a string')
    text = output_text or _extract_openai_output_text(data)
    usage = _optional_object(data, 'usage', 'OpenAI')
    input_details = _optional_object(usage, 'input_tokens_details', 'OpenAI')
    if not text:
        raise AIProviderError('OpenAI вернул пустой ответ.', code='empty_response')
    return AIProviderResult(
        text=text,
        input_tokens=_token_count(usage.get('input_tokens'), 'OpenAI', 'input_tokens'),
        cached_input_tokens=_token_count(
            input_details.get('cached_tokens'), 'OpenAI', 'cached_tokens',
        ),
        output_tokens=_token_count(usage.get('output_tokens'), 'OpenAI', 'output_tokens'),
        response_model=_response_model(data, model.external_id, 'OpenAI'),
    )


def _call_anthropic(
    model: AIModel,
    system_prompt: str,
    user_message: str,
    provider: ProviderDefinition,
) -> AIProviderResult:
    try:
        response = bounded_http_request(
            requests.post,
            f'{provider.base_url}/messages',
            headers={
                'x-api-key': provider.api_key,
                'anthropic-version': '2023-06-01',
                'Content-Type': 'application/json',
            },
            json={
                'model': model.external_id,
                'max_tokens': model.max_output_tokens,
                'system': system_prompt,
                'messages': [{'role': 'user', 'content': user_message}],
            },
            timeout=120,
            max_bytes=trusted_api_max_bytes(settings),
        )
    except TrustedResponseError as exc:
        raise AIProviderError(
            f'Anthropic вернул небезопасный ответ: {exc}',
            code='invalid_provider_response',
            retryable=False,
        ) from exc
    except requests.RequestException as exc:
        raise AIProviderError(
            f'Ошибка соединения с Anthropic: {exc}',
            code='connection_error',
            retryable=True,
        ) from exc
    data = _checked_json(response, 'Anthropic')
    content = _optional_list(data, 'content', 'Anthropic')
    chunks = []
    for item in content:
        if not isinstance(item, dict):
            _invalid_provider_response('Anthropic', 'content items must be objects')
        if item.get('type') != 'text':
            continue
        value = item.get('text')
        if not isinstance(value, str):
            _invalid_provider_response('Anthropic', 'text content must be a string')
        chunks.append(value)
    text = ''.join(chunks)
    usage = _optional_object(data, 'usage', 'Anthropic')
    if not text:
        raise AIProviderError('Anthropic вернул пустой ответ.', code='empty_response')
    return AIProviderResult(
        text=text,
        input_tokens=_token_count(usage.get('input_tokens'), 'Anthropic', 'input_tokens'),
        cached_input_tokens=_token_count(
            usage.get('cache_read_input_tokens'), 'Anthropic', 'cache_read_input_tokens',
        ),
        output_tokens=_token_count(usage.get('output_tokens'), 'Anthropic', 'output_tokens'),
        response_model=_response_model(data, model.external_id, 'Anthropic'),
    )


def _call_openai_compatible_chat(
    model: AIModel,
    system_prompt: str,
    user_message: str,
    provider: ProviderDefinition,
) -> AIProviderResult:
    try:
        response = bounded_http_request(
            requests.post,
            f'{provider.base_url}/chat/completions',
            headers={
                'Authorization': f'Bearer {provider.api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': model.external_id,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_message},
                ],
                'max_tokens': model.max_output_tokens,
                'stream': False,
            },
            timeout=120,
            max_bytes=trusted_api_max_bytes(settings),
        )
    except TrustedResponseError as exc:
        raise AIProviderError(
            f'{provider.display_name} вернул небезопасный ответ: {exc}',
            code='invalid_provider_response',
            retryable=False,
        ) from exc
    except requests.RequestException as exc:
        raise AIProviderError(
            f'Ошибка соединения с {provider.display_name}: {exc}',
            code='connection_error',
            retryable=True,
        ) from exc
    data = _checked_json(response, provider.display_name)
    choices = _optional_list(data, 'choices', provider.display_name)
    for choice in choices:
        if not isinstance(choice, dict):
            _invalid_provider_response(
                provider.display_name, 'choices items must be objects',
            )
    message = choices[0].get('message') if choices else {}
    if message is None:
        message = {}
    if not isinstance(message, dict):
        _invalid_provider_response(provider.display_name, 'message must be an object')
    text = _chat_content_as_text(message.get('content'), provider.display_name)
    usage = _optional_object(data, 'usage', provider.display_name)
    prompt_details = _optional_object(usage, 'prompt_tokens_details', provider.display_name)
    cached_tokens = (
        prompt_details.get('cached_tokens')
        or usage.get('prompt_cache_hit_tokens')
        or usage.get('cached_tokens')
        or 0
    )
    if not text:
        raise AIProviderError(
            f'{provider.display_name} вернул пустой ответ.',
            code='empty_response',
        )
    return AIProviderResult(
        text=text,
        input_tokens=_token_count(
            usage.get('prompt_tokens') or usage.get('input_tokens'),
            provider.display_name,
            'prompt_tokens',
        ),
        cached_input_tokens=_token_count(
            cached_tokens, provider.display_name, 'cached_tokens',
        ),
        output_tokens=_token_count(
            usage.get('completion_tokens') or usage.get('output_tokens'),
            provider.display_name,
            'completion_tokens',
        ),
        response_model=_response_model(data, model.external_id, provider.display_name),
    )


def _checked_json(response, provider_name: str) -> dict:
    try:
        data = response.json()
    except ValueError as exc:
        raise AIProviderError(
            f'{provider_name} вернул не-JSON ответ.',
            code='invalid_provider_response',
            retryable=response.status_code >= 500,
        ) from exc
    if not isinstance(data, dict):
        _invalid_provider_response(
            provider_name,
            'top-level JSON must be an object',
            retryable=response.status_code >= 500,
        )
    if response.status_code >= 400:
        error = data.get('error') or {}
        message = error.get('message') if isinstance(error, dict) else str(error)
        raise AIProviderError(
            f'{provider_name}: {message or response.status_code}',
            code=f'http_{response.status_code}',
            retryable=response.status_code == 429 or response.status_code >= 500,
        )
    return data


def _extract_openai_output_text(data: dict) -> str:
    chunks = []
    for item in _optional_list(data, 'output', 'OpenAI'):
        if not isinstance(item, dict):
            _invalid_provider_response('OpenAI', 'output items must be objects')
        if item.get('type') != 'message':
            continue
        for content in _optional_list(item, 'content', 'OpenAI'):
            if not isinstance(content, dict):
                _invalid_provider_response('OpenAI', 'content items must be objects')
            if content.get('type') != 'output_text':
                continue
            value = content.get('text')
            if value is not None and not isinstance(value, str):
                _invalid_provider_response('OpenAI', 'output text must be a string')
            if value:
                chunks.append(value)
    return ''.join(chunks)


def _chat_content_as_text(content, provider_name: str) -> str:
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        _invalid_provider_response(provider_name, 'message content must be a string or list')
    if len(content) > _MAX_AI_RESPONSE_ITEMS:
        _invalid_provider_response(provider_name, 'message content has too many items')
    chunks = []
    for item in content:
        if not isinstance(item, dict):
            _invalid_provider_response(provider_name, 'message content items must be objects')
        if item.get('type') not in {'text', 'output_text'}:
            continue
        value = item.get('text')
        if not isinstance(value, str):
            _invalid_provider_response(provider_name, 'message text must be a string')
        chunks.append(value)
    return ''.join(chunks)


def _optional_object(container: dict, key: str, provider_name: str) -> dict:
    value = container.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        _invalid_provider_response(provider_name, f'{key} must be an object')
    return value


def _optional_list(container: dict, key: str, provider_name: str) -> list:
    value = container.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        _invalid_provider_response(provider_name, f'{key} must be a list')
    if len(value) > _MAX_AI_RESPONSE_ITEMS:
        _invalid_provider_response(provider_name, f'{key} has too many items')
    return value


def _token_count(value, provider_name: str, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _invalid_provider_response(
            provider_name, f'{field_name} must be a non-negative integer',
        )
    return value


def _response_model(data: dict, fallback: str, provider_name: str) -> str:
    value = data.get('model')
    if value is None or value == '':
        return fallback
    if not isinstance(value, str):
        _invalid_provider_response(provider_name, 'model must be a string')
    return value


def _invalid_provider_response(
    provider_name: str,
    detail: str,
    *,
    retryable: bool = False,
) -> None:
    raise AIProviderError(
        f'{provider_name} вернул некорректный ответ: {detail}.',
        code='invalid_provider_response',
        retryable=retryable,
    )
