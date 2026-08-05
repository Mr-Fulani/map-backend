import json
import re
import time
from decimal import Decimal
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils.timezone import now

from apps.ai_agent.models import AIPromptTemplate, AIRequestLog, AITaskType
from apps.ai_agent.providers import call_model
from apps.ai_agent.routing import AIModelRouter
from apps.billing.ai_wallet import AIWalletService, InsufficientAICredits
from apps.billing.services import LimitChecker
from apps.core.url_security import is_safe_public_http_url
from apps.products.enrichment import make_value_hash, normalize_part_code
from apps.products.models import (
    ProductEnrichmentFact, VehicleFitment,
)
from apps.products.source_policy import should_auto_apply_fitment, should_auto_apply_record
from apps.web_research.models import (
    WebResearchClaim, WebResearchEvidence, WebResearchRun, WebSearchAttempt,
)
from apps.web_research.prompts import (
    WEB_RESEARCH_OUTPUT_SCHEMA, WEB_RESEARCH_SYSTEM_PROMPT,
)
from apps.web_research.providers.base import WebSearchProviderError
from apps.web_research.routing import search_provider_candidates


ZERO_CREDITS = Decimal('0')
SOURCE_ID = 'web_research'


class WebResearchUnavailable(RuntimeError):
    pass


class WebResearchValidationError(RuntimeError):
    pass


def enrichment_coverage(product) -> dict:
    """Deterministic coverage score used only to decide whether fallback is useful."""
    trusted_fitments = sum(
        1 for fitment in product.fitments.all() if should_auto_apply_fitment(fitment)
    )
    trusted_facts = sum(
        1 for fact in product.enrichment_facts.all() if should_auto_apply_record(fact)
    )
    score = 0.0
    score += 0.15 if product.brand else 0
    score += 0.10 if product.catalog_category_id or product.category_1c else 0
    score += 0.20 if product.cross_codes.exists() else 0
    score += 0.35 if trusted_fitments else 0
    score += 0.20 if product.attributes.exists() or trusted_facts else 0
    missing = []
    if not product.brand:
        missing.append('brand')
    if not product.cross_codes.exists():
        missing.append('oem_or_cross_codes')
    if not trusted_fitments:
        missing.append('fitments')
    if not product.attributes.exists() and not trusted_facts:
        missing.append('technical_facts')
    return {
        'score': round(score, 2),
        'threshold': float(settings.WEB_RESEARCH_COVERAGE_THRESHOLD),
        'missing': missing,
        'trusted_fitments': trusted_fitments,
        'trusted_facts': trusted_facts,
    }


def should_run_web_research(product) -> bool:
    if not getattr(settings, 'WEB_RESEARCH_AUTO_FALLBACK', True):
        return False
    coverage = enrichment_coverage(product)
    return coverage['score'] < coverage['threshold']


def build_research_queries(product) -> list[str]:
    article = str(product.article or '').strip()
    brand = str(product.brand or '').strip()
    name = str(product.name or '').strip()
    category = str(product.category_1c or '').strip()
    if not category and product.catalog_category_id:
        category = str(product.catalog_category.name or '').strip()

    internal_article = bool(
        article and re.sub(r'[^A-Za-z0-9]', '', article).upper().startswith('OEM')
    )
    queries = []
    if article and not internal_article:
        queries.append(' '.join(filter(None, [brand, f'"{article}"', name, 'автозапчасть'])))
    if name:
        clean_name = re.sub(re.escape(article), ' ', name, flags=re.IGNORECASE) if article else name
        queries.append(' '.join(filter(None, [brand, clean_name, category, 'автозапчасть'])))
    if article and internal_article and not name:
        queries.append(' '.join(filter(None, [f'"{article}"', category, 'автозапчасть'])))

    result = []
    seen = set()
    for query in queries:
        normalized = ' '.join(query.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
    return result[:max(1, settings.WEB_RESEARCH_MAX_QUERIES)]


class WebResearchService:
    @classmethod
    def create_run(
        cls, product, *, trigger: str, generate_after: bool = False,
        search_provider: str = '',
    ) -> tuple[WebResearchRun, bool]:
        coverage = enrichment_coverage(product)
        try:
            with transaction.atomic():
                run = WebResearchRun.objects.create(
                    tenant=product.tenant,
                    product=product,
                    trigger=trigger,
                    generate_after=generate_after,
                    search_provider=search_provider,
                    coverage_before=coverage,
                )
                return run, True
        except IntegrityError:
            run = WebResearchRun.objects.filter(
                product=product,
                status__in=[WebResearchRun.Status.QUEUED, WebResearchRun.Status.RUNNING],
            ).latest('created_at')
            if generate_after and not run.generate_after:
                run.generate_after = True
                run.save(update_fields=['generate_after', 'updated_at'])
            return run, False

    @classmethod
    def execute(cls, run_id: int) -> WebResearchRun:
        run = WebResearchRun.objects.select_related('tenant', 'product').get(pk=run_id)
        if run.status not in [WebResearchRun.Status.QUEUED, WebResearchRun.Status.RUNNING]:
            return run
        run.status = WebResearchRun.Status.RUNNING
        run.started_at = run.started_at or now()
        run.error_message = ''
        run.save(update_fields=['status', 'started_at', 'error_message', 'updated_at'])

        try:
            if not search_provider_candidates(run.tenant, run.search_provider):
                raise WebResearchUnavailable('Не настроен ни один провайдер интернет-поиска.')
            queries = build_research_queries(run.product)
            run.queries = queries
            run.save(update_fields=['queries', 'updated_at'])
            evidence, providers_used = cls._collect_evidence(run, queries)
            run.search_provider = providers_used[0] if providers_used else ''
            run.result_count = len(evidence)
            run.save(update_fields=['search_provider', 'result_count', 'updated_at'])
            if not evidence:
                finished = cls._finish(run, WebResearchRun.Status.NO_RESULTS)
                cls._generate_if_unblocked(finished)
                return finished

            extracted, model = WebResearchAgent().extract(run, evidence)
            run.ai_provider = model.provider
            run.ai_model = model.external_id
            with transaction.atomic():
                claims = cls._save_extracted_claims(run, extracted, evidence)
            run.claim_count = len(claims)
            run.coverage_after = enrichment_coverage(run.product)
            pending_claims = any(
                claim.review_status == WebResearchClaim.ReviewStatus.PENDING
                for claim in claims
            )
            status = (
                WebResearchRun.Status.NEED_REVIEW
                if pending_claims
                else WebResearchRun.Status.COMPLETED
                if claims
                else WebResearchRun.Status.NO_RESULTS
            )
            finished = cls._finish(run, status)
            cls._generate_if_unblocked(finished)
            return finished
        except Exception as exc:
            run.error_message = str(exc)[:2000]
            cls._finish(run, WebResearchRun.Status.FAILED)
            raise

    @staticmethod
    def _collect_evidence(run, queries) -> tuple[list[WebResearchEvidence], list[str]]:
        seen_urls = set(run.evidence.values_list('url', flat=True))
        evidence = list(run.evidence.all())
        providers_used = list(dict.fromkeys(
            item.provider_id for item in evidence if item.provider_id
        ))
        rank = len(evidence)
        last_error = None
        any_success = False
        for query in queries:
            results = []
            selected_provider = None
            for candidate in search_provider_candidates(run.tenant, run.search_provider):
                started = time.monotonic()
                try:
                    candidate_results = candidate.provider.search(
                        query, count=settings.WEB_RESEARCH_RESULTS_PER_QUERY,
                    )
                except WebSearchProviderError as exc:
                    last_error = exc
                    WebSearchAttempt.objects.create(
                        run=run,
                        connection=candidate.connection,
                        provider_id=candidate.provider.provider_id,
                        query=query[:500],
                        status=WebSearchAttempt.Status.FAILED,
                        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                        retryable=exc.retryable,
                        error_code=exc.code[:80],
                        error_message=str(exc)[:500],
                    )
                    continue
                attempt_status = (
                    WebSearchAttempt.Status.SUCCESS
                    if candidate_results else WebSearchAttempt.Status.EMPTY
                )
                WebSearchAttempt.objects.create(
                    run=run,
                    connection=candidate.connection,
                    provider_id=candidate.provider.provider_id,
                    query=query[:500],
                    status=attempt_status,
                    result_count=len(candidate_results),
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                )
                if candidate_results:
                    results = candidate_results
                    selected_provider = candidate.provider.provider_id
                    any_success = True
                    if selected_provider not in providers_used:
                        providers_used.append(selected_provider)
                    break
            for result in results:
                if result.url in seen_urls or not is_safe_public_http_url(result.url):
                    continue
                domain = (urlparse(result.url).hostname or '').lower()
                if not domain:
                    continue
                rank += 1
                item = WebResearchEvidence.objects.create(
                    run=run,
                    query=query[:500],
                    rank=rank,
                    provider_id=selected_provider or '',
                    title=result.title[:500],
                    url=result.url[:2000],
                    domain=domain[:255],
                    snippet=' '.join(filter(None, [
                        result.snippet, result.content[:4000],
                    ]))[:6000],
                )
                evidence.append(item)
                seen_urls.add(result.url)
        if not any_success and last_error is not None:
            raise last_error
        return evidence, providers_used

    @classmethod
    def _save_extracted_claims(cls, run, extracted, evidence):
        evidence_by_id = {item.pk: item for item in evidence}
        claims = []
        brand = str(extracted.get('brand') or '').strip()
        if brand:
            claim = cls._save_claim(
                run, WebResearchClaim.ClaimType.BRAND,
                {'brand': brand[:150]},
                extracted.get('brand_confidence'),
                extracted.get('brand_evidence_ids'), evidence_by_id,
            )
            if claim:
                fact = cls._save_fact(
                    run, ProductEnrichmentFact.FactType.BRAND,
                    'Предполагаемый бренд', brand, claim,
                )
                cls._link_saved(claim, fact)
                claims.append(claim)

        for item in list(extracted.get('cross_codes') or [])[:20]:
            code = str(item.get('code') or '').strip()
            if not normalize_part_code(code):
                continue
            payload = {
                'manufacturer': str(item.get('manufacturer') or '')[:100],
                'code': code[:100],
                'code_type': str(item.get('code_type') or 'Unknown'),
            }
            claim = cls._save_claim(
                run, WebResearchClaim.ClaimType.OEM, payload,
                item.get('confidence'), item.get('evidence_ids'), evidence_by_id,
            )
            if claim:
                fact = cls._save_fact(
                    run, ProductEnrichmentFact.FactType.OEM,
                    payload['manufacturer'] or payload['code_type'], payload['code'], claim,
                )
                cls._link_saved(claim, fact)
                claims.append(claim)

        for item in list(extracted.get('fitments') or [])[:30]:
            make = str(item.get('make') or '').strip()
            model = str(item.get('model') or '').strip()
            if not make or not model:
                continue
            raw_power_hp = item.get('power_hp')
            try:
                power_hp = int(raw_power_hp) if raw_power_hp is not None else None
            except (TypeError, ValueError):
                power_hp = None
            payload = {
                'make': make[:100],
                'model': model[:150],
                'generation': str(item.get('generation') or '')[:100],
                'date_from': str(item.get('date_from') or '')[:20],
                'date_to': str(item.get('date_to') or '')[:20],
                'modification': str(item.get('modification') or '')[:255],
                'engine_code': str(item.get('engine_code') or '')[:100],
                'power_hp': power_hp,
            }
            claim = cls._save_claim(
                run, WebResearchClaim.ClaimType.FITMENT, payload,
                item.get('confidence'), item.get('evidence_ids'), evidence_by_id,
            )
            if claim:
                first_url = claim.evidence.order_by('rank').values_list('url', flat=True).first() or ''
                fitment, created = VehicleFitment.objects.get_or_create(
                    tenant=run.tenant,
                    product=run.product,
                    source_id=SOURCE_ID,
                    make=payload['make'],
                    model=payload['model'],
                    generation=payload['generation'],
                    modification=payload['modification'],
                    engine_code=payload['engine_code'],
                    power_hp=payload['power_hp'],
                    defaults={
                        'source_url': first_url,
                        'date_from': payload['date_from'],
                        'date_to': payload['date_to'],
                        'raw_text': cls._evidence_summary(claim),
                        'confidence': claim.confidence,
                        'needs_review': True,
                        'last_seen_at': now(),
                    },
                )
                if (
                    not created
                    and not fitment.needs_review
                    and fitment.review_status == 'approved'
                ):
                    claim.review_status = WebResearchClaim.ReviewStatus.APPROVED
                    claim.save(update_fields=['review_status', 'updated_at'])
                cls._link_saved(claim, fitment)
                claims.append(claim)

        allowed_fact_types = {
            ProductEnrichmentFact.FactType.TECHNICAL,
            ProductEnrichmentFact.FactType.DESCRIPTION_HINT,
            ProductEnrichmentFact.FactType.WARNING,
        }
        for item in list(extracted.get('facts') or [])[:30]:
            fact_type = str(item.get('fact_type') or '')
            name = str(item.get('name') or '').strip()
            value = str(item.get('value') or '').strip()
            if fact_type not in allowed_fact_types or not name or not value:
                continue
            payload = {'fact_type': fact_type, 'name': name[:150], 'value': value[:3000]}
            claim = cls._save_claim(
                run, WebResearchClaim.ClaimType.FACT, payload,
                item.get('confidence'), item.get('evidence_ids'), evidence_by_id,
            )
            if claim:
                fact = cls._save_fact(run, fact_type, payload['name'], payload['value'], claim)
                cls._link_saved(claim, fact)
                claims.append(claim)
        return claims

    @staticmethod
    def _save_claim(run, claim_type, payload, confidence, evidence_ids, evidence_by_id):
        selected = [
            evidence_by_id[evidence_id]
            for evidence_id in dict.fromkeys(evidence_ids or [])
            if evidence_id in evidence_by_id
        ]
        if not selected:
            return None
        domains = {item.domain for item in selected}
        cap = 0.70 if len(domains) >= 2 else 0.55
        try:
            normalized_confidence = max(0.0, min(float(confidence or 0), cap))
        except (TypeError, ValueError):
            normalized_confidence = 0.0
        claim = WebResearchClaim.objects.create(
            run=run,
            claim_type=claim_type,
            payload=payload,
            confidence=normalized_confidence,
        )
        claim.evidence.set(selected)
        return claim

    @classmethod
    def _save_fact(cls, run, fact_type, name, value, claim):
        first_url = claim.evidence.order_by('rank').values_list('url', flat=True).first() or ''
        fact, created = ProductEnrichmentFact.objects.get_or_create(
            tenant=run.tenant,
            product=run.product,
            source_id=SOURCE_ID,
            fact_type=fact_type,
            name=name[:150],
            value_hash=make_value_hash(value),
            defaults={
                'source_url': first_url,
                'value': value,
                'raw_text': cls._evidence_summary(claim),
                'confidence': claim.confidence,
                'needs_review': True,
                'last_seen_at': now(),
            },
        )
        if (
            not created
            and not fact.needs_review
            and fact.review_status == 'approved'
        ):
            claim.review_status = WebResearchClaim.ReviewStatus.APPROVED
            claim.save(update_fields=['review_status', 'updated_at'])
        return fact

    @staticmethod
    def _evidence_summary(claim) -> str:
        return json.dumps({
            'claim_payload': claim.payload,
            'evidence': [
                {'id': item.pk, 'url': item.url, 'title': item.title}
                for item in claim.evidence.order_by('rank')
            ],
        }, ensure_ascii=False)

    @staticmethod
    def _link_saved(claim, record):
        claim.saved_model = record._meta.label_lower
        claim.saved_record_id = record.pk
        claim.save(update_fields=['saved_model', 'saved_record_id', 'updated_at'])

    @staticmethod
    def _finish(run, status):
        run.status = status
        run.finished_at = now()
        run.save(update_fields=[
            'status', 'result_count', 'claim_count', 'ai_provider', 'ai_model',
            'coverage_after', 'error_message', 'finished_at', 'updated_at',
        ])
        return run

    @staticmethod
    def _generate_if_unblocked(run):
        # Claims from open-web evidence always require review. Generating before
        # approval would silently omit them from the grounded description context.
        if not run.generate_after or run.status == WebResearchRun.Status.NEED_REVIEW:
            return
        from apps.ai_agent.tasks import generate_description_task
        generate_description_task.delay(run.product_id)

    @classmethod
    def record_claim_review(cls, record, review_status: str) -> None:
        """Synchronize product review actions with the research run lifecycle."""
        model_label = record._meta.label_lower
        claims = WebResearchClaim.objects.filter(
            saved_model=model_label,
            saved_record_id=record.pk,
            review_status=WebResearchClaim.ReviewStatus.PENDING,
        )
        run_ids = list(claims.values_list('run_id', flat=True).distinct())
        normalized = (
            WebResearchClaim.ReviewStatus.APPROVED
            if review_status == 'approved'
            else WebResearchClaim.ReviewStatus.REJECTED
        )
        claims.update(review_status=normalized, updated_at=now())
        for run_id in run_ids:
            with transaction.atomic():
                run = WebResearchRun.objects.select_for_update().get(pk=run_id)
                if run.status != WebResearchRun.Status.NEED_REVIEW:
                    continue
                if run.claims.filter(
                    review_status=WebResearchClaim.ReviewStatus.PENDING,
                ).exists():
                    continue
                run.status = WebResearchRun.Status.COMPLETED
                run.save(update_fields=['status', 'updated_at'])
            cls._generate_if_unblocked(run)


class WebResearchAgent:
    def extract(self, run, evidence):
        can, reason = LimitChecker().can_generate_ai(run.tenant)
        if not can:
            raise WebResearchUnavailable(reason)
        prompt = self._prompt()
        message = self._message(run, evidence)
        last_error = None
        for model in AIModelRouter.candidates(run.tenant, AITaskType.WEB_RESEARCH):
            estimated_input = max(1, (len(prompt) + len(message)) // 4)
            estimated_credits = model.estimate_credits(estimated_input, model.max_output_tokens)
            try:
                reservation = AIWalletService.reserve(
                    run.tenant,
                    estimated_credits,
                    details={
                        'task_type': AITaskType.WEB_RESEARCH,
                        'provider': model.provider,
                        'model': model.external_id,
                        'research_run_id': run.pk,
                    },
                )
            except InsufficientAICredits as exc:
                last_error = exc
                continue
            started = time.monotonic()
            try:
                provider_result = call_model(
                    model, prompt, message, output_schema=WEB_RESEARCH_OUTPUT_SCHEMA,
                )
                parsed = self._parse_response(provider_result.text)
            except Exception as exc:
                AIWalletService.release(run.tenant, reservation, reason='web_research_failed')
                self._log(run, model, 'error', started, error=str(exc))
                last_error = exc
                continue

            actual = model.calculate_credits(
                input_tokens=provider_result.input_tokens,
                cached_input_tokens=provider_result.cached_input_tokens,
                output_tokens=provider_result.output_tokens,
            )
            charged = AIWalletService.settle(
                run.tenant,
                reservation,
                actual,
                details={
                    'task_type': AITaskType.WEB_RESEARCH,
                    'provider': model.provider,
                    'model': model.external_id,
                    'research_run_id': run.pk,
                },
            )
            type(run.tenant).objects.filter(pk=run.tenant_id).update(
                ai_credits_used=F('ai_credits_used') + 1,
            )
            self._log(
                run, model, 'success', started,
                input_tokens=provider_result.input_tokens,
                cached_input_tokens=provider_result.cached_input_tokens,
                output_tokens=provider_result.output_tokens,
                charged=charged,
            )
            return parsed, model
        raise WebResearchUnavailable(f'AI-модель не выполнила исследование: {last_error}')

    @staticmethod
    def _prompt():
        template = AIPromptTemplate.objects.filter(
            task_type=AITaskType.WEB_RESEARCH,
            catalog_domain='auto_parts',
            marketplace='',
            is_active=True,
        ).first()
        instructions = template.system_prompt if template else WEB_RESEARCH_SYSTEM_PROMPT
        return (
            instructions
            + '\nВерни только JSON следующей структуры, без Markdown:\n'
            + json.dumps(WEB_RESEARCH_OUTPUT_SCHEMA, ensure_ascii=False)
        )

    @staticmethod
    def _message(run, evidence):
        payload = {
            'task': 'grounded_auto_part_web_research',
            'product': {
                'article': run.product.article,
                'brand': run.product.brand,
                'name': run.product.name,
                'category': run.product.category_1c,
            },
            'evidence': [
                {
                    'evidence_id': item.pk,
                    'domain': item.domain,
                    'url': item.url,
                    'title': item.title,
                    'snippet': item.snippet,
                }
                for item in evidence
            ],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))

    @staticmethod
    def _parse_response(text):
        cleaned = str(text or '').strip()
        if cleaned.startswith('```'):
            cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', cleaned, flags=re.IGNORECASE)
        try:
            parsed = json.loads(cleaned)
        except (TypeError, ValueError) as exc:
            raise WebResearchValidationError('AI вернул невалидный JSON.') from exc
        required = {'brand', 'brand_evidence_ids', 'cross_codes', 'fitments', 'facts'}
        if not isinstance(parsed, dict) or not required <= set(parsed):
            raise WebResearchValidationError('AI вернул неполную структуру исследования.')
        if not isinstance(parsed['brand'], str):
            raise WebResearchValidationError('AI вернул неверный тип бренда.')
        if not WebResearchAgent._is_evidence_id_list(parsed['brand_evidence_ids']):
            raise WebResearchValidationError('AI вернул неверные ссылки на доказательства.')
        for collection_name in ('cross_codes', 'fitments', 'facts'):
            collection = parsed[collection_name]
            if not isinstance(collection, list) or any(
                not isinstance(item, dict)
                or not WebResearchAgent._is_evidence_id_list(item.get('evidence_ids'))
                for item in collection
            ):
                raise WebResearchValidationError(
                    f'AI вернул неверную структуру поля {collection_name}.',
                )
        return parsed

    @staticmethod
    def _is_evidence_id_list(value):
        return isinstance(value, list) and all(
            isinstance(item, int) and not isinstance(item, bool) for item in value
        )

    @staticmethod
    def _log(
        run, model, status, started, *, error='', input_tokens=0,
        cached_input_tokens=0, output_tokens=0, charged=ZERO_CREDITS,
    ):
        AIRequestLog.objects.create(
            tenant=run.tenant,
            task_type=AITaskType.WEB_RESEARCH,
            provider=model.provider,
            model_id=model.external_id,
            status=status,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            charged_credits=charged,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            error_code='web_research_error' if error else '',
            error_message=error[:500],
        )
