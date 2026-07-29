import time
from decimal import Decimal

from django.db.models import F

from apps.ai_agent.enrichment_context import ProductAIEnrichmentContextBuilder
from apps.ai_agent.models import AIRequestLog, AITaskType
from apps.ai_agent.prompts import SYSTEM_PROMPT
from apps.ai_agent.providers import AIProviderError, call_model
from apps.ai_agent.routing import AIModelRouter
from apps.ai_agent.validators import (
    BannedWordsError,
    VagueFitmentError,
    ValidationError,
    validate_json_response,
)
from apps.billing.ai_wallet import AIWalletService, InsufficientAICredits
from apps.billing.services import LimitChecker

MAX_VALIDATION_RETRIES = 2
ZERO_CREDITS = Decimal('0')


class AICreditsExhausted(Exception):
    """AI-кредиты тенанта исчерпаны."""


class DescriptionAgent:
    """Генерирует описание выбранной тенантом моделью с автоматическим fallback."""

    task_type = AITaskType.DESCRIPTION

    def generate(self, product, tenant, variation_index: int = 0) -> dict:
        can, reason = LimitChecker().can_generate_ai(tenant)
        if not can:
            raise AICreditsExhausted(reason)

        candidates = AIModelRouter.candidates(tenant, self.task_type)
        last_error = None
        for candidate_index, model in enumerate(candidates):
            attempts = MAX_VALIDATION_RETRIES if candidate_index == 0 else 1
            for _attempt in range(attempts):
                message = self._build_message(product, variation_index)
                estimated_input = max(1, (len(SYSTEM_PROMPT) + len(message)) // 4)
                estimated_credits = model.estimate_credits(
                    estimated_input,
                    model.max_output_tokens,
                )
                try:
                    reservation = AIWalletService.reserve(
                        tenant,
                        estimated_credits,
                        details={
                            'task_type': self.task_type,
                            'provider': model.provider,
                            'model': model.external_id,
                        },
                    )
                except InsufficientAICredits as exc:
                    last_error = str(exc)
                    continue

                started = time.monotonic()
                try:
                    provider_result = call_model(model, SYSTEM_PROMPT, message)
                    result = validate_json_response(provider_result.text)
                except (BannedWordsError, VagueFitmentError, ValidationError) as exc:
                    AIWalletService.release(
                        tenant, reservation, reason='validation_rejected',
                    )
                    self._log_request(
                        tenant=tenant,
                        model=model,
                        status=AIRequestLog.STATUS_REJECTED,
                        duration_ms=self._duration_ms(started),
                        error_code='validation_rejected',
                        error_message=str(exc),
                    )
                    variation_index += 1
                    last_error = str(exc)
                    continue
                except AIProviderError as exc:
                    AIWalletService.release(
                        tenant, reservation, reason=exc.code,
                    )
                    self._log_request(
                        tenant=tenant,
                        model=model,
                        status=AIRequestLog.STATUS_ERROR,
                        duration_ms=self._duration_ms(started),
                        error_code=exc.code,
                        error_message=str(exc),
                    )
                    last_error = str(exc)
                    break
                except Exception as exc:
                    AIWalletService.release(
                        tenant, reservation, reason='unexpected_error',
                    )
                    self._log_request(
                        tenant=tenant,
                        model=model,
                        status=AIRequestLog.STATUS_ERROR,
                        duration_ms=self._duration_ms(started),
                        error_code='unexpected_error',
                        error_message=str(exc),
                    )
                    last_error = str(exc)
                    break

                actual_credits = model.calculate_credits(
                    input_tokens=provider_result.input_tokens,
                    cached_input_tokens=provider_result.cached_input_tokens,
                    output_tokens=provider_result.output_tokens,
                )
                charged = AIWalletService.settle(
                    tenant,
                    reservation,
                    actual_credits,
                    details={
                        'task_type': self.task_type,
                        'provider': model.provider,
                        'model': model.external_id,
                        'input_tokens': provider_result.input_tokens,
                        'cached_input_tokens': provider_result.cached_input_tokens,
                        'output_tokens': provider_result.output_tokens,
                    },
                )
                self._increment_credits(tenant)
                self._log_request(
                    tenant=tenant,
                    model=model,
                    status=AIRequestLog.STATUS_SUCCESS,
                    duration_ms=self._duration_ms(started),
                    input_tokens=provider_result.input_tokens,
                    cached_input_tokens=provider_result.cached_input_tokens,
                    output_tokens=provider_result.output_tokens,
                    charged_credits=charged,
                )
                result['provider'] = model.provider
                result['model'] = model.external_id
                result['charged_credits'] = str(charged)
                return result

        if last_error and 'Недостаточно AI-кредитов' in last_error:
            raise AICreditsExhausted(last_error)
        raise RuntimeError(f'AI-модели не смогли сгенерировать описание: {last_error}')

    def _build_message(self, product, variation_index: int = 0) -> str:
        """Формирует текст запроса к модели из полей товара."""
        enrichment_context = ProductAIEnrichmentContextBuilder().build(product)
        lines = [
            f'Артикул: {product.article}',
            f'Название: {product.name}',
            f'Бренд: {product.brand}' if product.brand else '',
            f'Категория: {product.category_1c}' if product.category_1c else '',
            f'Состояние: {product.get_condition_display()}',
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
            lines.append(
                f'\nЭто вариант #{variation_index + 1}. Используй другую структуру первого абзаца.'
            )
        return '\n'.join(line for line in lines if line)

    @staticmethod
    def _duration_ms(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    def _log_request(
        self,
        *,
        tenant,
        model,
        status,
        duration_ms,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        charged_credits=ZERO_CREDITS,
        error_code='',
        error_message='',
    ) -> None:
        AIRequestLog.objects.create(
            tenant=tenant,
            task_type=self.task_type,
            provider=model.provider,
            model_id=model.external_id,
            status=status,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            charged_credits=charged_credits,
            duration_ms=duration_ms,
            error_code=error_code,
            error_message=error_message[:500],
        )

    @staticmethod
    def _increment_credits(tenant):
        """Сохраняет legacy-счётчик успешных генераций для старой аналитики."""
        from apps.tenants.models import Tenant

        Tenant.objects.filter(pk=tenant.pk).update(
            ai_credits_used=F('ai_credits_used') + 1,
        )
