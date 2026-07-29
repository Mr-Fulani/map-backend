from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class ProviderDefinition:
    code: str
    display_name: str
    api_style: str
    base_url: str
    api_key_setting: str

    @property
    def api_key(self) -> str:
        return str(getattr(settings, self.api_key_setting, '') or '')


PROVIDERS = {
    'openai': ProviderDefinition(
        code='openai',
        display_name='OpenAI',
        api_style='openai_responses',
        base_url='https://api.openai.com/v1',
        api_key_setting='OPENAI_API_KEY',
    ),
    'anthropic': ProviderDefinition(
        code='anthropic',
        display_name='Anthropic',
        api_style='anthropic_messages',
        base_url='https://api.anthropic.com/v1',
        api_key_setting='ANTHROPIC_API_KEY',
    ),
    'gemini': ProviderDefinition(
        code='gemini',
        display_name='Google Gemini',
        api_style='openai_chat',
        base_url='https://generativelanguage.googleapis.com/v1beta/openai',
        api_key_setting='GEMINI_API_KEY',
    ),
    'deepseek': ProviderDefinition(
        code='deepseek',
        display_name='DeepSeek',
        api_style='openai_chat',
        base_url='https://api.deepseek.com',
        api_key_setting='DEEPSEEK_API_KEY',
    ),
    'kimi': ProviderDefinition(
        code='kimi',
        display_name='Kimi',
        api_style='openai_chat',
        base_url='https://api.moonshot.ai/v1',
        api_key_setting='MOONSHOT_API_KEY',
    ),
}


def get_provider(code: str) -> ProviderDefinition | None:
    return PROVIDERS.get(code)


def provider_is_configured(code: str) -> bool:
    provider = get_provider(code)
    return bool(provider and provider.api_key)


def provider_choices() -> list[tuple[str, str]]:
    return [(provider.code, provider.display_name) for provider in PROVIDERS.values()]
