from dataclasses import dataclass

import requests

from apps.ai_agent.models import AIModel
from apps.ai_agent.provider_registry import ProviderDefinition, get_provider


class AIProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = 'provider_error', retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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
        response = requests.post(
            f'{provider.base_url}/responses',
            headers={
                'Authorization': f'Bearer {provider.api_key}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=120,
        )
    except requests.RequestException as exc:
        raise AIProviderError(
            f'Ошибка соединения с OpenAI: {exc}',
            code='connection_error',
            retryable=True,
        ) from exc
    data = _checked_json(response, 'OpenAI')
    text = data.get('output_text') or _extract_openai_output_text(data)
    usage = data.get('usage') or {}
    input_details = usage.get('input_tokens_details') or {}
    if not text:
        raise AIProviderError('OpenAI вернул пустой ответ.', code='empty_response')
    return AIProviderResult(
        text=text,
        input_tokens=int(usage.get('input_tokens') or 0),
        cached_input_tokens=int(input_details.get('cached_tokens') or 0),
        output_tokens=int(usage.get('output_tokens') or 0),
        response_model=str(data.get('model') or model.external_id),
    )


def _call_anthropic(
    model: AIModel,
    system_prompt: str,
    user_message: str,
    provider: ProviderDefinition,
) -> AIProviderResult:
    try:
        response = requests.post(
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
        )
    except requests.RequestException as exc:
        raise AIProviderError(
            f'Ошибка соединения с Anthropic: {exc}',
            code='connection_error',
            retryable=True,
        ) from exc
    data = _checked_json(response, 'Anthropic')
    text = ''.join(
        item.get('text', '')
        for item in data.get('content', [])
        if item.get('type') == 'text'
    )
    usage = data.get('usage') or {}
    if not text:
        raise AIProviderError('Anthropic вернул пустой ответ.', code='empty_response')
    return AIProviderResult(
        text=text,
        input_tokens=int(usage.get('input_tokens') or 0),
        cached_input_tokens=int(usage.get('cache_read_input_tokens') or 0),
        output_tokens=int(usage.get('output_tokens') or 0),
        response_model=str(data.get('model') or model.external_id),
    )


def _call_openai_compatible_chat(
    model: AIModel,
    system_prompt: str,
    user_message: str,
    provider: ProviderDefinition,
) -> AIProviderResult:
    try:
        response = requests.post(
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
        )
    except requests.RequestException as exc:
        raise AIProviderError(
            f'Ошибка соединения с {provider.display_name}: {exc}',
            code='connection_error',
            retryable=True,
        ) from exc
    data = _checked_json(response, provider.display_name)
    choices = data.get('choices') or []
    message = choices[0].get('message') if choices else {}
    text = _chat_content_as_text((message or {}).get('content'))
    usage = data.get('usage') or {}
    prompt_details = usage.get('prompt_tokens_details') or {}
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
        input_tokens=int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0),
        cached_input_tokens=int(cached_tokens),
        output_tokens=int(
            usage.get('completion_tokens') or usage.get('output_tokens') or 0,
        ),
        response_model=str(data.get('model') or model.external_id),
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
    for item in data.get('output', []):
        if item.get('type') != 'message':
            continue
        for content in item.get('content', []):
            if content.get('type') == 'output_text' and content.get('text'):
                chunks.append(content['text'])
    return ''.join(chunks)


def _chat_content_as_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    return ''.join(
        item.get('text', '')
        for item in content
        if isinstance(item, dict) and item.get('type') in {'text', 'output_text'}
    )
