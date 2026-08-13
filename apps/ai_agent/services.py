import json
import re
import time
from decimal import Decimal

from django.db.models import F

from apps.ai_agent.enrichment_context import ProductAIEnrichmentContextBuilder
from apps.ai_agent.models import AIProviderOperation, AIRequestLog, AITaskType
from apps.ai_agent.prompting import PromptSelection, resolve_description_prompt
from apps.ai_agent.providers import AIProviderError, call_model
from apps.ai_agent.reconciliation import (
    begin_ai_provider_operation, mark_ai_provider_network_started,
    mark_ai_provider_operation_uncertain, release_ai_provider_operation,
    settle_ai_provider_operation,
)
from apps.ai_agent.routing import AIModelRouter
from apps.ai_agent.validators import (
    BannedWordsError,
    VagueFitmentError,
    ValidationError,
    validate_json_response,
)
from apps.billing.ai_wallet import InsufficientAICredits
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
        if not candidates:
            # No reservation/provider boundary exists yet. Let the durable
            # dispatch retry configuration recovery without misclassifying
            # this as an unknown paid outcome.
            from apps.core.dispatch import SafeRetryableDispatchError
            raise SafeRetryableDispatchError(
                'No configured AI model is currently available.',
            )
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
                    operation = begin_ai_provider_operation(
                        tenant=tenant,
                        task_type=self.task_type,
                        provider=model.provider,
                        model_id=model.external_id,
                        reserved_amount=estimated_credits,
                        domain_type=AIProviderOperation.DomainType.PRODUCT,
                        domain_reference=str(product.pk),
                        reservation_details={
                            'task_type': self.task_type,
                            'provider': model.provider,
                            'model': model.external_id,
                        },
                    )
                except InsufficientAICredits as exc:
                    last_error = str(exc)
                    continue

                started = time.monotonic()
                mark_ai_provider_network_started(operation.pk)
                try:
                    provider_result = call_model(
                        model,
                        prompt.system_prompt,
                        message,
                        output_schema=prompt.output_schema,
                    )
                    result = validate_json_response(
                        provider_result.text,
                        protected_tokens=self._protected_identifiers(product),
                    )
                    self._validate_required_identity(product, result)
                    self._validate_required_fitments(product, result)
                    self._validate_required_cross_codes(product, result)
                    self._validate_rich_description(product, result)
                except (BannedWordsError, VagueFitmentError, ValidationError) as exc:
                    try:
                        actual_credits = model.calculate_credits(
                            input_tokens=provider_result.input_tokens,
                            cached_input_tokens=provider_result.cached_input_tokens,
                            output_tokens=provider_result.output_tokens,
                        )
                        operation, charged = settle_ai_provider_operation(
                            operation.pk,
                            actual_amount=actual_credits,
                            terminal_reason='validation_rejected',
                            details={
                                'task_type': self.task_type,
                                'provider': model.provider,
                                'model': model.external_id,
                                'input_tokens': provider_result.input_tokens,
                                'cached_input_tokens': provider_result.cached_input_tokens,
                                'output_tokens': provider_result.output_tokens,
                                'validation_rejected': True,
                            },
                        )
                    except Exception as settlement_exc:
                        mark_ai_provider_operation_uncertain(
                            operation.pk,
                            error_code='settlement_failed',
                        )
                        self._log_request(
                            tenant=tenant,
                            model=model,
                            status=AIRequestLog.STATUS_ERROR,
                            duration_ms=self._duration_ms(started),
                            input_tokens=provider_result.input_tokens,
                            cached_input_tokens=provider_result.cached_input_tokens,
                            output_tokens=provider_result.output_tokens,
                            error_code='provider_settlement_uncertain',
                            error_message=str(settlement_exc),
                            prompt_selection=prompt,
                        )
                        raise RuntimeError(
                            'Не удалось подтвердить списание AI-кредитов; '
                            'операция передана на сверку.',
                        ) from settlement_exc
                    self._log_request(
                        tenant=tenant,
                        model=model,
                        status=AIRequestLog.STATUS_REJECTED,
                        duration_ms=self._duration_ms(started),
                        input_tokens=provider_result.input_tokens,
                        cached_input_tokens=provider_result.cached_input_tokens,
                        output_tokens=provider_result.output_tokens,
                        charged_credits=charged,
                        error_code='validation_rejected',
                        error_message=str(exc),
                        prompt_selection=prompt,
                    )
                    variation_index += 1
                    previous_validation_error = str(exc)
                    last_error = str(exc)
                    continue
                except AIProviderError as exc:
                    if exc.request_not_accepted:
                        release_ai_provider_operation(
                            operation.pk, reason=exc.code,
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
                    mark_ai_provider_operation_uncertain(
                        operation.pk,
                        error_code=exc.code,
                    )
                    self._log_request(
                        tenant=tenant,
                        model=model,
                        status=AIRequestLog.STATUS_ERROR,
                        duration_ms=self._duration_ms(started),
                        error_code='provider_outcome_uncertain',
                        error_message=str(exc),
                        prompt_selection=prompt,
                    )
                    # Once the network boundary has been crossed, fallback is
                    # allowed only with authoritative non-acceptance evidence.
                    raise RuntimeError(
                        'Результат AI-провайдера неизвестен; автоматический '
                        'повтор запрещён.',
                    ) from exc
                except Exception as exc:
                    mark_ai_provider_operation_uncertain(
                        operation.pk,
                        error_code='post_provider_failure',
                    )
                    self._log_request(
                        tenant=tenant,
                        model=model,
                        status=AIRequestLog.STATUS_ERROR,
                        duration_ms=self._duration_ms(started),
                        error_code='provider_outcome_uncertain',
                        error_message=str(exc),
                        prompt_selection=prompt,
                    )
                    raise RuntimeError(
                        'Ошибка после начала AI-запроса; операция передана на '
                        'сверку, автоматический повтор запрещён.',
                    ) from exc

                try:
                    result['model_confidence'] = result['confidence']
                    result['confidence'] = self.calculate_grounding_confidence(product)
                    result['provider'] = model.provider
                    result['model'] = model.external_id
                    result['prompt_version'] = prompt.version
                    actual_credits = model.calculate_credits(
                        input_tokens=provider_result.input_tokens,
                        cached_input_tokens=provider_result.cached_input_tokens,
                        output_tokens=provider_result.output_tokens,
                    )
                    operation, charged = settle_ai_provider_operation(
                        operation.pk,
                        actual_amount=actual_credits,
                        details={
                            'task_type': self.task_type,
                            'provider': model.provider,
                            'model': model.external_id,
                            'input_tokens': provider_result.input_tokens,
                            'cached_input_tokens': provider_result.cached_input_tokens,
                            'output_tokens': provider_result.output_tokens,
                        },
                        validated_result=result,
                        apply_required=True,
                    )
                except Exception as exc:
                    mark_ai_provider_operation_uncertain(
                        operation.pk,
                        error_code='settlement_failed',
                    )
                    self._log_request(
                        tenant=tenant,
                        model=model,
                        status=AIRequestLog.STATUS_ERROR,
                        duration_ms=self._duration_ms(started),
                        input_tokens=provider_result.input_tokens,
                        cached_input_tokens=provider_result.cached_input_tokens,
                        output_tokens=provider_result.output_tokens,
                        error_code='provider_settlement_uncertain',
                        error_message=str(exc),
                        prompt_selection=prompt,
                    )
                    raise RuntimeError(
                        'Не удалось подтвердить списание AI-кредитов; '
                        'операция передана на сверку.',
                    ) from exc
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
                result['charged_credits'] = str(charged)
                result['_provider_operation_id'] = str(operation.pk)
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
        """Keep exact short fitments or their truthful compact model-family summary."""
        context = ProductAIEnrichmentContextBuilder().build(product)
        if not context.trusted_fitments:
            return

        combined = ''.join(
            character for character in f'{result["title"]} {result["description"]}'.casefold()
            if character.isalnum()
        )
        presentation = context.fitment_presentation
        if presentation.get('mode') == 'compact':
            required_values = [
                *presentation.get('required_makes', []),
                *presentation.get('required_models', []),
            ]
            missing = [
                value for value in required_values
                if ''.join(char for char in value.casefold() if char.isalnum()) not in combined
            ]
            if missing:
                raise ValidationError(
                    'Ответ потерял марки или классы из компактной '
                    'применяемости: ' + ', '.join(missing[:10])
                )
            return

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
    def _protected_identifiers(product) -> tuple[str, ...]:
        identifiers = {str(product.article or '').strip()}
        for code, normalized_code in product.cross_codes.values_list(
            'code', 'normalized_code',
        ):
            identifiers.update({str(code or '').strip(), str(normalized_code or '').strip()})
        return tuple(sorted((value for value in identifiers if value), key=len, reverse=True))

    @staticmethod
    def _validate_required_cross_codes(product, result: dict) -> None:
        """Reject descriptions that omit or mutilate every confirmed cross-code."""
        codes = {
            ''.join(character for character in str(value or '').casefold() if character.isalnum())
            for pair in product.cross_codes.values_list('code', 'normalized_code')
            for value in pair
        }
        codes.discard('')
        if not codes:
            return
        description = ''.join(
            character
            for character in str(result['description']).casefold()
            if character.isalnum()
        )
        if not any(code in description for code in codes):
            raise ValidationError('Ответ потерял подтверждённые OEM/Cross-коды.')

    @staticmethod
    def _validate_rich_description(product, result: dict) -> None:
        """Prevent a richly enriched product from collapsing into two generic lines."""
        context = ProductAIEnrichmentContextBuilder().build(product)
        profile = context.content_profile
        if profile.get('level') != 'rich':
            return

        description = str(result.get('description') or '')
        normalized = ''.join(character for character in description.casefold() if character.isalnum())
        if len(description) < 350:
            raise ValidationError(
                'Для товара с полным обогащением описание должно содержать '
                'не менее 350 символов полезных фактов.'
            )
        article = ''.join(
            character for character in str(product.article or '').casefold()
            if character.isalnum()
        )
        if article and article not in normalized:
            raise ValidationError(
                'Полное описание должно повторять артикул товара.'
            )

        description_lower = description.casefold()
        section_markers = {
            'compatibility': 'совместимост',
            'specifications': 'характеристик',
            'catalog_numbers': 'номер',
            'condition': 'состояни',
        }
        missing_sections = [
            section
            for section in profile.get('available_sections', [])
            if section in section_markers
            and section_markers[section] not in description_lower
        ]
        if missing_sections:
            raise ValidationError(
                'Полное описание потеряло доступные разделы: '
                + ', '.join(missing_sections)
            )

        from apps.products.attribute_presentation import presented_attributes
        duplicate_wva = []
        for _item, name, value in presented_attributes(
            product.attributes.all(),
            for_ai=True,
        ):
            if name.casefold() != 'wva':
                continue
            for code in re.findall(r'[A-ZА-ЯЁ0-9]{4,}', value.upper()):
                normalized_code = ''.join(
                    character for character in code.casefold()
                    if character.isalnum()
                )
                if normalized.count(normalized_code) > 1:
                    duplicate_wva.append(code)
        if duplicate_wva:
            raise ValidationError(
                'Описание повторяет WVA-номера: '
                + ', '.join(sorted(set(duplicate_wva)))
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
