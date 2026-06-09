import anthropic
from django.conf import settings
from django.db.models import F

from apps.ai_agent.enrichment_context import ProductAIEnrichmentContextBuilder
from apps.ai_agent.prompts import SYSTEM_PROMPT
from apps.ai_agent.validators import (
    BannedWordsError, VagueFitmentError, ValidationError, validate_json_response,
)
from apps.billing.services import LimitChecker

MAX_RETRIES = 2  # Максимум попыток через Claude перед fallback на OpenAI


class AICreditsExhausted(Exception):
    """AI-кредиты тенанта исчерпаны."""


class DescriptionAgent:
    """Агент генерации описаний объявлений через Claude (fallback — GPT-4o)."""

    def generate(self, product, tenant, variation_index: int = 0) -> dict:
        """
        Генерирует описание для товара.

        Сначала пробует Claude (до MAX_RETRIES попыток при BannedWordsError),
        затем fallback на OpenAI если Claude недоступен.
        Атомарно инкрементирует ai_credits_used тенанта.
        """
        can, reason = LimitChecker().can_generate_ai(tenant)
        if not can:
            raise AICreditsExhausted(reason)

        try:
            plan_slug = tenant.subscription.plan.slug
        except Exception:
            plan_slug = 'starter'

        last_error = None
        for _attempt in range(MAX_RETRIES):
            try:
                result = self._call_claude(product, plan_slug, variation_index)
                self._increment_credits(tenant)
                return result
            except (BannedWordsError, VagueFitmentError):
                # Запрещённые слова — меняем вариацию и пробуем снова
                variation_index += 1
                last_error = 'invalid_description'
                continue
            except (anthropic.APIError, anthropic.RateLimitError, anthropic.APIConnectionError):
                break
            except ValidationError:
                break

        try:
            result = self._call_openai(product, plan_slug, variation_index)
            self._increment_credits(tenant)
            return result
        except Exception as e:
            raise RuntimeError(f'Claude и OpenAI недоступны: {e}. Последняя ошибка: {last_error}')

    def _build_message(self, product, variation_index: int = 0) -> str:
        """Формирует текст запроса к модели из полей товара."""
        enrichment_context = ProductAIEnrichmentContextBuilder().build(product)
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
        if enrichment_context.has_context:
            lines.extend([
                '',
                'Данные обогащения из каталогов:',
                *enrichment_context.to_prompt_lines(),
                (
                    'Используй эти данные как факты. Если есть строка "Подходит к автомобилям", '
                    'обязательно укажи эти автомобили в первом абзаце: марка, модель, поколение '
                    'и модификация, если они переданы.'
                ),
                (
                    'Фразу "также подходит" используй только для автомобилей из строки '
                    '"Подходит к автомобилям".'
                ),
                (
                    'Если есть только строка "Вероятные марки авто по OEM/Cross", можно указать '
                    'только эти марки и написать, что совместимость нужно сверить по OEM/Cross. '
                    'Модели и поколения не придумывай.'
                ),
            ])
        if variation_index > 0:
            lines.append(f'\nЭто вариант #{variation_index + 1}. Используй другую структуру первого абзаца.')
        return '\n'.join(line for line in lines if line)

    def _call_claude(self, product, plan_slug: str, variation_index: int = 0) -> dict:
        """Вызывает Claude API и парсит JSON-ответ."""
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        response = client.messages.create(
            model='claude-3-5-sonnet-latest' if plan_slug == 'pro' else 'claude-3-5-haiku-latest',
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{'role': 'user', 'content': self._build_message(product, variation_index)}],
        )
        return validate_json_response(response.content[0].text)

    def _call_openai(self, product, plan_slug: str, variation_index: int = 0) -> dict:
        """Вызывает OpenAI API (fallback если Claude недоступен)."""
        from openai import OpenAI
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model='gpt-4o' if plan_slug == 'pro' else 'gpt-4o-mini',
            max_tokens=2048,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': self._build_message(product, variation_index)},
            ],
        )
        return validate_json_response(response.choices[0].message.content)

    @staticmethod
    def _increment_credits(tenant):
        """Атомарно увеличивает счётчик AI-кредитов через F() — без race condition."""
        from apps.tenants.models import Tenant
        Tenant.objects.filter(pk=tenant.pk).update(ai_credits_used=F('ai_credits_used') + 1)
