import json
import time
from decimal import Decimal

from django.db.models import F

from apps.ai_agent.enrichment_context import ProductAIEnrichmentContextBuilder
from apps.ai_agent.models import AIRequestLog, AITaskType
from apps.ai_agent.prompting import PromptSelection, resolve_description_prompt
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
        previous_validation_error = ''
        prompt = resolve_description_prompt(product)
        for candidate_index, model in enumerate(candidates):
            attempts = MAX_VALIDATION_RETRIES if candidate_index == 0 else 1
            for _attempt in range(attempts):
                message = self._build_message(
                    product,
                    variation_index,
                    previous_validation_error=previous_validation_error,
                )
                estimated_input = max(1, (len(prompt.system_prompt) + len(message)) // 4)
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
                    provider_result = call_model(
                        model,
                        prompt.system_prompt,
                        message,
                        output_schema=prompt.output_schema,
                    )
                    result = validate_json_response(provider_result.text)
                    self._validate_required_identity(product, result)
                    self._validate_required_fitments(product, result)
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
                        prompt_selection=prompt,
                    )
                    variation_index += 1
                    previous_validation_error = str(exc)
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
                        prompt_selection=prompt,
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
                        prompt_selection=prompt,
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
                    prompt_selection=prompt,
                )
                result['model_confidence'] = result['confidence']
                result['confidence'] = self.calculate_grounding_confidence(product)
                result['provider'] = model.provider
                result['model'] = model.external_id
                result['prompt_version'] = prompt.version
                result['charged_credits'] = str(charged)
                return result

        if last_error and 'Недостаточно AI-кредитов' in last_error:
            raise AICreditsExhausted(last_error)
        raise RuntimeError(f'AI-модели не смогли сгенерировать описание: {last_error}')

    def _build_message(
        self,
        product,
        variation_index: int = 0,
        *,
        previous_validation_error: str = '',
    ) -> str:
        """Serialize untrusted catalogue data instead of mixing it with instructions."""
        enrichment_context = ProductAIEnrichmentContextBuilder().build(product)
        payload = {
            'task': 'marketplace_product_description',
            'product_data': {
                'article': product.article,
                'name': product.name,
                'brand': product.brand,
                'category': product.category_1c,
                'condition': product.get_condition_display(),
                'description_1c': (product.description_1c or '')[:5000],
            },
            'enrichment': enrichment_context.to_prompt_payload(),
            'variation': variation_index + 1,
        }
        if previous_validation_error:
            payload['retry_feedback'] = {
                'previous_response_rejected': True,
                'reason': previous_validation_error[:500],
                'required_action': 'Исправь указанную ошибку, сохранив только подтверждённые факты.',
            }
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))

    @staticmethod
    def _validate_required_identity(product, result: dict) -> None:
        combined = f'{result["title"]} {result["description"]}'.casefold()
        normalized = ''.join(character for character in combined if character.isalnum())
        article = ''.join(
            character for character in str(product.article or '').casefold()
            if character.isalnum()
        )
        brand = str(product.brand or '').strip().casefold()
        if article and article not in normalized:
            raise ValidationError('Ответ потерял артикул товара.')
        if brand and brand not in combined:
            raise ValidationError('Ответ потерял бренд товара.')

    @staticmethod
    def _validate_required_fitments(product, result: dict) -> None:
        """Do not accept an answer that drops confirmed vehicle compatibility."""
        context = ProductAIEnrichmentContextBuilder().build(product)
        if not context.trusted_fitments:
            return

        combined = ''.join(
            character for character in f'{result["title"]} {result["description"]}'.casefold()
            if character.isalnum()
        )
        missing = []
        seen = set()
        for fitment in context.trusted_fitments:
            make = str(fitment.get('make') or '').strip()
            model = str(fitment.get('model') or '').strip()
            generation = str(fitment.get('generation') or '').strip()
            key = (make.casefold(), model.casefold(), generation.casefold())
            if key in seen:
                continue
            seen.add(key)
            required = [value for value in [make, model, generation] if value]
            if any(
                ''.join(char for char in value.casefold() if char.isalnum()) not in combined
                for value in required
            ):
                missing.append(' '.join(required))
        if missing:
            raise ValidationError(
                'Ответ потерял подтверждённую применяемость: '
                + ', '.join(missing[:10])
            )

    @staticmethod
    def calculate_grounding_confidence(product) -> float:
        """Confidence reflects source completeness, not the model's self-assessment."""
        score = 0.35
        score += 0.15 if product.article else 0
        score += 0.10 if product.brand else 0
        score += 0.05 if product.name else 0
        score += 0.05 if product.category_1c or product.catalog_category_id else 0
        score += 0.05 if product.description_1c else 0
        score += 0.05 if product.attributes.exists() else 0
        score += 0.05 if product.cross_codes.exists() else 0
        score += 0.15 if product.fitments.filter(
            needs_review=False, confidence__gte=0.8,
        ).exists() else 0
        score += 0.05 if product.enrichment_facts.filter(
            needs_review=False, confidence__gte=0.8,
        ).exists() else 0
        return round(min(score, 0.98), 2)

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
        prompt_selection: PromptSelection | None = None,
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
            prompt_template=prompt_selection.template if prompt_selection else None,
            prompt_version=prompt_selection.version if prompt_selection else '',
            prompt_hash=prompt_selection.sha256 if prompt_selection else '',
        )

    @staticmethod
    def _increment_credits(tenant):
        """Сохраняет legacy-счётчик успешных генераций для старой аналитики."""
        from apps.tenants.models import Tenant

        Tenant.objects.filter(pk=tenant.pk).update(
            ai_credits_used=F('ai_credits_used') + 1,
        )
