import anthropic
from django.conf import settings
from django.db.models import F

from apps.ai_agent.prompts import SYSTEM_PROMPT
from apps.ai_agent.validators import BannedWordsError, ValidationError, validate_json_response
from apps.billing.services import LimitChecker

MAX_RETRIES = 2


class AICreditsExhausted(Exception):
    pass


class DescriptionAgent:
    def generate(self, product, tenant, variation_index: int = 0) -> dict:
        can, reason = LimitChecker().can_generate_ai(tenant)
        if not can:
            raise AICreditsExhausted(reason)

        last_error = None
        for _attempt in range(MAX_RETRIES):
            try:
                result = self._call_claude(product, variation_index)
                self._increment_credits(tenant)
                return result
            except BannedWordsError:
                variation_index += 1
                last_error = 'banned_words'
                continue
            except (anthropic.APIError, anthropic.RateLimitError, anthropic.APIConnectionError):
                break
            except ValidationError:
                break

        try:
            result = self._call_openai(product, variation_index)
            self._increment_credits(tenant)
            return result
        except Exception as e:
            raise RuntimeError(f'Claude и OpenAI недоступны: {e}. Последняя ошибка: {last_error}')

    def _build_message(self, product, variation_index: int = 0) -> str:
        lines = [
            f'Артикул: {product.article}',
            f'Название: {product.name}',
            f'Бренд: {product.brand}' if product.brand else '',
            f'Категория: {product.category_1c}' if product.category_1c else '',
            f'Состояние: {product.get_condition_display()}',
            f'Цена: {product.price} ₽',
            f'Остаток: {product.stock_qty} шт.',
        ]
        if product.description_1c:
            lines.append(f'Описание из 1С: {product.description_1c}')
        if variation_index > 0:
            lines.append(f'\nЭто вариант #{variation_index + 1}. Используй другую структуру первого абзаца.')
        return '\n'.join(line for line in lines if line)

    def _call_claude(self, product, variation_index: int = 0) -> dict:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': self._build_message(product, variation_index)}],
        )
        return validate_json_response(response.content[0].text)

    def _call_openai(self, product, variation_index: int = 0) -> dict:
        from openai import OpenAI
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model='gpt-4o',
            max_tokens=1000,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': self._build_message(product, variation_index)},
            ],
        )
        return validate_json_response(response.choices[0].message.content)

    @staticmethod
    def _increment_credits(tenant):
        from apps.tenants.models import Tenant
        Tenant.objects.filter(pk=tenant.pk).update(ai_credits_used=F('ai_credits_used') + 1)
