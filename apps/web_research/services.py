import json
import re
import time
from dataclasses import asdict
from decimal import Decimal
from functools import partial
from typing import TypedDict
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import (
    BigIntegerField, Case, Exists, F, OuterRef, Q, Value, When,
)
from django.db.models.functions import Cast
from django.utils.timezone import now

from apps.ai_agent.models import (
    AIProviderOperation, AIPromptTemplate, AIRequestLog, AITaskType,
)
from apps.ai_agent.providers import AIProviderError, call_model
from apps.ai_agent.reconciliation import (
    begin_ai_provider_operation, mark_ai_provider_network_started,
    mark_ai_provider_operation_uncertain, release_ai_provider_operation,
    settle_ai_provider_operation,
)
from apps.ai_agent.routing import AIModelRouter
from apps.billing.ai_wallet import InsufficientAICredits
from apps.billing.services import LimitChecker
from apps.core.url_security import is_safe_public_http_url
from apps.products.enrichment import make_value_hash, normalize_part_code
from apps.products.models import (
    ProductEnrichmentFact, VehicleFitment,
)
from apps.products.source_policy import should_auto_apply_fitment, should_auto_apply_record
from apps.web_research.models import (
    CompetitorOffer, WebResearchClaim, WebResearchEvidence, WebResearchRun,
    WebSearchAttempt, WebSearchConnection, WebSearchWorkflow,
)
from apps.web_research.accounting import (
    acknowledge_web_search_workflow, acquire_web_search_workflow,
    deterministic_web_search_call_key, execute_recorded_web_search,
    fingerprint_web_search_request, replay_recorded_web_search,
    release_empty_web_search_workflow, resume_web_search_workflow,
)
from apps.web_research.offer_extraction import save_deterministic_offers
from apps.web_research.prompts import (
    WEB_RESEARCH_OUTPUT_SCHEMA, WEB_RESEARCH_SYSTEM_PROMPT,
)
from apps.web_research.providers.base import WebSearchProviderError
from apps.web_research.providers.base import WebSearchResult
from apps.web_research.providers.registry import registered_search_providers
from apps.web_research.routing import search_provider_candidates
from apps.web_research.search_context import (
    build_search_contexts, get_tenant_research_settings, localize_query,
    result_matches_context, SearchContext, search_contexts_from_snapshot,
)


ZERO_CREDITS = Decimal('0')
SOURCE_ID = 'web_research'


class _FitmentPayload(TypedDict):
    make: str
    model: str
    generation: str
    date_from: str
    date_to: str
    modification: str
    engine_code: str
    power_hp: int | None


class WebResearchUnavailable(RuntimeError):
    pass


class WebResearchReconciliationRequired(WebResearchUnavailable):
    """An earlier paid AI outcome for this product still needs a decision."""


class WebSearchOutcomeUncertain(WebResearchUnavailable):
    """A paid search request may have been accepted and must not be replayed."""

    outcome_uncertain = True


class WebResearchTerminalSearchFailure(WebResearchUnavailable):
    """The immutable provider plan ended authoritatively without evidence."""


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


def web_research_domain_reference(product, purpose: str) -> str:
    """Return the stable product/purpose identity used by paid-search fences."""
    purpose_family = (
        'pricing'
        if purpose in [
            WebResearchRun.Purpose.PRICING,
            WebResearchRun.Purpose.COMBINED,
        ]
        else 'enrichment'
    )
    return f'product:{product.pk}:purpose:{purpose_family}'


def _web_search_results_to_checkpoint(results: list[WebSearchResult]) -> list[dict]:
    """Normalize provider dataclasses into bounded encrypted JSON evidence."""
    return [asdict(result) for result in results]


def _web_search_results_from_checkpoint(value: object) -> list[WebSearchResult]:
    if not isinstance(value, list):
        raise ValueError('web-search result checkpoint must be a list')
    restored = []
    fields = {
        'title', 'url', 'snippet', 'rank', 'content', 'raw_content',
        'score', 'published_at', 'metadata',
    }
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('web-search result checkpoint item must be an object')
        payload = {key: item[key] for key in fields if key in item}
        restored.append(WebSearchResult(**payload))
    return restored


def _search_context_from_plan(value: object) -> SearchContext:
    if not isinstance(value, dict):
        raise WebResearchUnavailable('Неизменяемый план поиска повреждён.')
    try:
        return SearchContext(
            country_code=str(value.get('country_code') or ''),
            language=str(value.get('language') or 'ru'),
            include_domains=tuple(value.get('include_domains') or []),
            exclude_domains=tuple(value.get('exclude_domains') or []),
            market_intent=str(value.get('market_intent') or 'enrichment'),
            strict_region=bool(value.get('strict_region', True)),
            result_limit=int(value.get('result_limit') or 20),
        )
    except (TypeError, ValueError) as exc:
        raise WebResearchUnavailable('Неизменяемый план поиска повреждён.') from exc


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


def build_pricing_queries(product) -> list[str]:
    """Commercial queries prioritize stable article/OEM identifiers."""
    brand = str(product.brand or '').strip()
    codes = [str(product.article or '').strip()]
    codes.extend(str(value or '').strip() for value in product.oem_numbers or [])
    codes.extend(str(value or '').strip() for value in product.cross_numbers or [])
    codes.extend(product.cross_codes.values_list('code', flat=True)[:8])
    queries = []
    seen = set()
    for code in codes:
        normalized = normalize_part_code(code)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        queries.append(' '.join(filter(None, [brand, f'"{code}"', product.name])))
    if not queries:
        queries = build_research_queries(product)
    return queries[:max(1, settings.WEB_RESEARCH_MAX_QUERIES)]


class WebResearchService:
    @staticmethod
    def _workflow_supports_local_resume(
        workflow: WebSearchWorkflow | None,
        *,
        run_status: str,
    ) -> bool:
        """Whether provider work can be resumed without another network call.

        ``APPLIED`` means every recorded search result has been durably
        consumed into this run's evidence rows.  Pricing/AI/final run writes
        can still be pending, so that local phase remains replayable from the
        canonical evidence even though the provider-domain fence is closed.
        """
        if workflow is None:
            return False
        if workflow.status in {
            WebSearchWorkflow.Status.IN_PROGRESS,
            WebSearchWorkflow.Status.APPLY_PENDING,
        }:
            return True
        if workflow.status != WebSearchWorkflow.Status.APPLIED:
            return False
        attempts = workflow.attempts.all()
        if not attempts.exists():
            return run_status in {
                WebResearchRun.Status.QUEUED,
                WebResearchRun.Status.RUNNING,
            }
        return attempts.filter(
            status__in=[
                WebSearchAttempt.Status.SUCCESS,
                WebSearchAttempt.Status.EMPTY,
            ],
            checkpoint_enc__isnull=False,
        ).exists()

    @staticmethod
    def _has_unresolved_web_ai(run: WebResearchRun) -> bool:
        return AIProviderOperation.objects.filter(
            tenant_id=run.tenant_id,
            task_type=AITaskType.WEB_RESEARCH,
            domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
            domain_reference=str(run.pk),
            status__in=[
                AIProviderOperation.Status.RESERVED,
                AIProviderOperation.Status.PENDING_RECONCILIATION,
            ],
        ).exists()

    @staticmethod
    def _has_terminal_nonapplicable_web_ai(run: WebResearchRun) -> bool:
        return AIProviderOperation.objects.filter(
            tenant_id=run.tenant_id,
            task_type=AITaskType.WEB_RESEARCH,
            domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
            domain_reference=str(run.pk),
        ).filter(
            Q(status=AIProviderOperation.Status.RELEASED)
            | Q(
                status=AIProviderOperation.Status.SETTLED,
                apply_state=AIProviderOperation.ApplyState.NOT_REQUIRED,
            )
        ).exists()

    @staticmethod
    def _build_search_workflow_plan(run, base_queries, contexts, candidates) -> dict:
        provider_plan = []
        for candidate in candidates:
            parameters = getattr(candidate.provider, 'parameters', {})
            provider_plan.append({
                'provider_id': candidate.provider.provider_id,
                'connection_id': (
                    candidate.connection.pk if candidate.connection else None
                ),
                'parameters': parameters if isinstance(parameters, dict) else {},
            })
        result_limit = int(settings.WEB_RESEARCH_RESULTS_PER_QUERY)
        slots = []
        for context_index, context in enumerate(contexts):
            for query_index, base_query in enumerate(base_queries):
                query = localize_query(base_query, context)
                slots.append({
                    'slot': f'context:{context_index}:query:{query_index}',
                    'query': query,
                    'context': context.to_snapshot(),
                    'count': min(result_limit, context.result_limit),
                })
        return {
            'version': 1,
            'run_id': run.pk,
            'product_id': run.product_id,
            'purpose': run.purpose,
            'providers': provider_plan,
            'slots': slots,
        }

    @classmethod
    def _search_workflow(cls, run, base_queries, contexts):
        workflow_key = f'web-research-run:{run.pk}'
        try:
            return resume_web_search_workflow(
                tenant=run.tenant,
                operation='web_research',
                workflow_key=workflow_key,
            )
        except WebSearchWorkflow.DoesNotExist:
            candidates = search_provider_candidates(
                run.tenant, run.search_provider,
            )
            if not candidates:
                raise WebResearchUnavailable(
                    'Не настроен ни один провайдер интернет-поиска.',
                )
            plan = cls._build_search_workflow_plan(
                run, base_queries, contexts, candidates,
            )
            return acquire_web_search_workflow(
                tenant=run.tenant,
                product=run.product,
                run=run,
                operation='web_research',
                domain_reference=web_research_domain_reference(
                    run.product, run.purpose,
                ),
                workflow_key=workflow_key,
                input_snapshot=plan,
            )

    @staticmethod
    def _provider_from_plan(run, provider_plan):
        if not isinstance(provider_plan, dict):
            raise WebResearchUnavailable('Неизменяемый план поиска повреждён.')
        provider_id = str(provider_plan.get('provider_id') or '').strip().lower()
        registry = registered_search_providers()
        provider_class = registry.get(provider_id)
        if provider_class is None:
            raise WebSearchProviderError(
                'Запланированный поисковый провайдер недоступен до отправки.',
                retryable=False,
                code='pre_send_failure',
            )
        parameters = provider_plan.get('parameters')
        if not isinstance(parameters, dict):
            raise WebResearchUnavailable('Неизменяемый план поиска повреждён.')
        connection_id = provider_plan.get('connection_id')
        for candidate in search_provider_candidates(run.tenant, run.search_provider):
            candidate_connection_id = (
                candidate.connection.pk if candidate.connection else None
            )
            candidate_parameters = getattr(candidate.provider, 'parameters', {})
            if not isinstance(candidate_parameters, dict):
                candidate_parameters = {}
            if (
                candidate.provider.provider_id == provider_id
                and candidate_connection_id == connection_id
                and candidate_parameters == parameters
            ):
                return candidate.provider, candidate.connection
        connection = None
        credentials = None
        if connection_id is not None:
            connection = WebSearchConnection.objects.filter(
                pk=connection_id,
                provider_id=provider_id,
                is_active=True,
            ).first()
            if connection is None:
                raise WebSearchProviderError(
                    'Запланированное подключение недоступно до отправки.',
                    retryable=False,
                    code='pre_send_failure',
                )
            credentials = connection.get_credentials()
        elif WebSearchConnection.objects.filter(provider_id=provider_id).exists():
            raise WebSearchProviderError(
                'Запланированный env-провайдер теперь управляется в БД и недоступен.',
                retryable=False,
                code='pre_send_failure',
            )
        provider = provider_class(
            credentials=credentials,
            parameters=parameters,
        )
        if not provider.is_available():
            raise WebSearchProviderError(
                'Запланированный поисковый провайдер недоступен до отправки.',
                retryable=False,
                code='pre_send_failure',
            )
        return provider, connection

    @staticmethod
    def _execute_provider_search(provider, query, count, context):
        return provider.search(query, count=count, context=context)

    @classmethod
    def create_run(
        cls, product, *, trigger: str, generate_after: bool = False,
        search_provider: str = '', purpose: str = WebResearchRun.Purpose.ENRICHMENT,
        consume_daily_budget: bool = True,
        origin_key: str = '',
    ) -> tuple[WebResearchRun, bool]:
        coverage = enrichment_coverage(product)
        tenant_settings = get_tenant_research_settings(product.tenant)
        contexts = build_search_contexts(tenant_settings, purpose=purpose)
        settings_snapshot = tenant_settings.snapshot()
        settings_snapshot['country_codes'] = [
            context.country_code for context in contexts if context.country_code
        ]
        settings_snapshot['search_contexts'] = [context.to_snapshot() for context in contexts]
        normalized_origin = str(origin_key).strip()[:160]
        try:
            with transaction.atomic():
                type(product.tenant).objects.select_for_update().only('pk').get(
                    pk=product.tenant_id,
                )
                type(product).objects.select_for_update().only('pk').get(pk=product.pk)
                if normalized_origin:
                    canonical = WebResearchRun.objects.filter(
                        tenant=product.tenant,
                        origin_key=normalized_origin,
                    ).first()
                    if canonical is not None:
                        if (
                            canonical.product_id != product.pk
                            or canonical.purpose != purpose
                            or canonical.search_provider != search_provider
                        ):
                            raise ValueError(
                                'Web research origin key conflicts with canonical request.'
                            )
                        upgraded_generate = generate_after and not canonical.generate_after
                        if upgraded_generate:
                            canonical.generate_after = True
                            canonical.save(update_fields=['generate_after', 'updated_at'])
                            if canonical.status in {
                                WebResearchRun.Status.COMPLETED,
                                WebResearchRun.Status.NO_RESULTS,
                                WebResearchRun.Status.SKIPPED,
                            }:
                                cls._generate_if_unblocked(canonical)
                        return canonical, False
                search_domain = web_research_domain_reference(product, purpose)
                blocking_search_workflow = WebSearchWorkflow.objects.filter(
                    tenant=product.tenant,
                    operation='web_research',
                ).filter(
                    Q(domain_reference=search_domain)
                    | Q(domain_reference__startswith=f'{search_domain}:legacy:')
                ).filter(
                    Q(status__in=WebSearchWorkflow.ACTIVE_STATUSES)
                    | Q(
                        status=WebSearchWorkflow.Status.APPLIED,
                        run__status__in=[
                            WebResearchRun.Status.QUEUED,
                            WebResearchRun.Status.RUNNING,
                        ],
                    )
                    | Q(
                        status=WebSearchWorkflow.Status.APPLIED,
                        run__status=WebResearchRun.Status.FAILED,
                        attempts__status__in=[
                            WebSearchAttempt.Status.SUCCESS,
                            WebSearchAttempt.Status.EMPTY,
                        ],
                    )
                )
                if blocking_search_workflow.exists():
                    raise WebResearchReconciliationRequired(
                        'Предыдущий платный поиск по товару требует сверки.',
                    )
                matching_product_run = WebResearchRun.objects.filter(
                    tenant=product.tenant,
                    product=product,
                    pk=OuterRef('_numeric_run_id'),
                )
                unresolved_ai = AIProviderOperation.objects.filter(
                    tenant=product.tenant,
                    task_type=AITaskType.WEB_RESEARCH,
                    domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
                ).filter(
                    Q(status__in=[
                        AIProviderOperation.Status.RESERVED,
                        AIProviderOperation.Status.PENDING_RECONCILIATION,
                    ])
                    | Q(
                        status=AIProviderOperation.Status.SETTLED,
                        apply_state=AIProviderOperation.ApplyState.PENDING,
                    ),
                ).annotate(
                    _numeric_run_id=Case(
                        When(
                            domain_reference__regex=r'^[0-9]{1,18}$',
                            then=Cast(
                                'domain_reference',
                                output_field=BigIntegerField(),
                            ),
                        ),
                        default=Value(None),
                        output_field=BigIntegerField(),
                    ),
                ).annotate(
                    _belongs_to_product=Exists(matching_product_run),
                ).filter(
                    # A malformed unresolved audit reference cannot be
                    # attributed safely. Fail closed for this tenant rather
                    # than silently allowing another paid operation.
                    Q(_numeric_run_id__isnull=True)
                    | Q(_belongs_to_product=True),
                )
                if unresolved_ai.exists():
                    raise WebResearchReconciliationRequired(
                        'Предыдущая операция интернет-исследования требует сверки.',
                    )
                run = WebResearchRun.objects.create(
                    tenant=product.tenant,
                    product=product,
                    trigger=trigger,
                    purpose=purpose,
                    settings_snapshot=settings_snapshot,
                    generate_after=generate_after,
                    search_provider=search_provider,
                    coverage_before=coverage,
                    origin_key=normalized_origin,
                )
                if consume_daily_budget:
                    from apps.core.throttling import (
                        consume_transactional_tenant_daily_budget,
                    )
                    consume_transactional_tenant_daily_budget(
                        tenant=product.tenant,
                        scope='web-research-starts',
                        cost=1,
                        limit=settings.WEB_RESEARCH_TENANT_DAILY_STARTS,
                    )
                return run, True
        except IntegrityError:
            active_runs = WebResearchRun.objects.filter(
                product=product,
                status__in=[WebResearchRun.Status.QUEUED, WebResearchRun.Status.RUNNING],
            )
            if purpose in [WebResearchRun.Purpose.PRICING, WebResearchRun.Purpose.COMBINED]:
                active_runs = active_runs.filter(purpose__in=[
                    WebResearchRun.Purpose.PRICING, WebResearchRun.Purpose.COMBINED,
                ])
            else:
                active_runs = active_runs.filter(purpose=WebResearchRun.Purpose.ENRICHMENT)
            run = active_runs.latest('created_at')
            if generate_after and not run.generate_after:
                run.generate_after = True
                run.save(update_fields=['generate_after', 'updated_at'])
            return run, False

    @classmethod
    def execute(cls, run_id: int) -> WebResearchRun:
        from apps.core.advisory_lock import try_session_advisory_lock
        from apps.core.dispatch import SafeRetryableDispatchError

        with try_session_advisory_lock(
            f'web-search-workflow:{run_id}',
        ) as acquired:
            if not acquired:
                raise SafeRetryableDispatchError(
                    'Web-research workflow is already owned by another worker.',
                )
            return cls._execute_owned(run_id)

    @classmethod
    def _execute_owned(cls, run_id: int) -> WebResearchRun:
        run = WebResearchRun.objects.select_related('tenant', 'product').get(pk=run_id)
        pending_operation_id = (
            AIProviderOperation.objects.filter(
                tenant_id=run.tenant_id,
                task_type=AITaskType.WEB_RESEARCH,
                domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
                domain_reference=str(run.pk),
                status=AIProviderOperation.Status.SETTLED,
                apply_state=AIProviderOperation.ApplyState.PENDING,
            )
            .order_by('created_at')
            .values_list('pk', flat=True)
            .first()
        )
        if pending_operation_id is not None:
            return cls.apply_ai_provider_operation(pending_operation_id)
        search_workflow = WebSearchWorkflow.objects.filter(
            tenant_id=run.tenant_id,
            run_id=run.pk,
            operation='web_research',
        ).order_by('-created_at', '-pk').first()
        if cls._has_unresolved_web_ai(run):
            run.error_message = (
                'AI provider operation requires explicit reconciliation.'
            )
            cls._finish(run, WebResearchRun.Status.FAILED)
            raise WebSearchOutcomeUncertain(
                'Результат AI-провайдера требует сверки; '
                'локальное продолжение заблокировано.',
            )
        if run.status not in [WebResearchRun.Status.QUEUED, WebResearchRun.Status.RUNNING]:
            # A local database/apply failure used to mark the run FAILED after
            # a paid checkpoint had already committed. The active workflow is
            # authoritative: resume its exact checkpoint without another
            # provider call instead of stranding paid evidence.
            if (
                run.status == WebResearchRun.Status.FAILED
                and cls._workflow_supports_local_resume(
                    search_workflow,
                    run_status=run.status,
                )
                and not cls._has_unresolved_web_ai(run)
            ):
                run.status = WebResearchRun.Status.RUNNING
            else:
                return run
        run.status = WebResearchRun.Status.RUNNING
        run.started_at = run.started_at or now()
        run.error_message = ''
        run.save(update_fields=['status', 'started_at', 'error_message', 'updated_at'])

        try:
            contexts = search_contexts_from_snapshot(
                run.settings_snapshot, purpose=run.purpose,
            )
            query_builder = (
                build_pricing_queries
                if run.purpose in [WebResearchRun.Purpose.PRICING, WebResearchRun.Purpose.COMBINED]
                else build_research_queries
            )
            base_queries = query_builder(run.product)
            workflow = cls._search_workflow(run, base_queries, contexts)
            plan = workflow.input_snapshot
            if not isinstance(plan, dict) or plan.get('version') != 1:
                raise WebResearchUnavailable('Неизменяемый план поиска повреждён.')
            slots = plan.get('slots')
            if not isinstance(slots, list):
                raise WebResearchUnavailable('Неизменяемый план поиска повреждён.')
            run.queries = list(dict.fromkeys(
                str(slot.get('query') or '')
                for slot in slots if isinstance(slot, dict)
            ))
            run.save(update_fields=['queries', 'updated_at'])
            if workflow.status == WebSearchWorkflow.Status.APPLIED:
                evidence = list(run.evidence.all())
                providers_used = list(dict.fromkeys(
                    item.provider_id for item in evidence if item.provider_id
                ))
            elif workflow.status == WebSearchWorkflow.Status.RECONCILED:
                raise WebResearchUnavailable(
                    'Исход платного поиска был закрыт оператором без результата.',
                )
            else:
                evidence, providers_used = cls._collect_evidence(run, workflow)
            if not evidence:
                with transaction.atomic():
                    finished = cls._finish(run, WebResearchRun.Status.NO_RESULTS)
                    cls._generate_if_unblocked(finished)
                return finished

            claims: list[WebResearchClaim] = []
            offers: list[CompetitorOffer] = []
            if run.purpose in [WebResearchRun.Purpose.PRICING, WebResearchRun.Purpose.COMBINED]:
                offers = save_deterministic_offers(
                    run, evidence,
                    ttl_hours=int(run.settings_snapshot.get('price_ttl_hours') or 24),
                )
                run.offer_count = len(offers)
            if (
                run.purpose
                in [WebResearchRun.Purpose.ENRICHMENT, WebResearchRun.Purpose.COMBINED]
                and not cls._has_terminal_nonapplicable_web_ai(run)
            ):
                extracted, _model = WebResearchAgent().extract(run, evidence)
                operation_id = extracted.pop('_provider_operation_id', None)
                if operation_id is None:
                    raise WebResearchUnavailable(
                        'AI-результат не связан с durable provider operation.',
                    )
                return cls.apply_ai_provider_operation(operation_id)
            run.claim_count = len(claims)
            run.coverage_after = enrichment_coverage(run.product)
            pending_claims = any(
                claim.review_status == WebResearchClaim.ReviewStatus.PENDING
                for claim in claims
            )
            pending_offers = any(
                offer.review_status == CompetitorOffer.ReviewStatus.PENDING
                for offer in offers
            )
            status = (
                WebResearchRun.Status.NEED_REVIEW
                if pending_claims or pending_offers
                else WebResearchRun.Status.COMPLETED
                if claims or offers
                else WebResearchRun.Status.NO_RESULTS
            )
            finished = cls._finish(run, status)
            cls._generate_if_unblocked(finished)
            return finished
        except Exception as exc:
            run.error_message = str(exc)[:2000]
            search_workflow = WebSearchWorkflow.objects.filter(
                tenant_id=run.tenant_id,
                run_id=run.pk,
                operation='web_research',
            ).order_by('-created_at', '-pk').first()
            unresolved_web_ai = cls._has_unresolved_web_ai(run)
            if (
                not isinstance(exc, WebResearchTerminalSearchFailure)
                and cls._workflow_supports_local_resume(
                    search_workflow,
                    run_status=run.status,
                )
                and not unresolved_web_ai
            ):
                # The provider plan/checkpoint remains replayable. Persist a
                # retryable domain state; never terminalize a run merely
                # because its local evidence/offer/AI apply failed.
                run.status = WebResearchRun.Status.QUEUED
                run.finished_at = None
                run.save(update_fields=[
                    'status', 'error_message', 'finished_at', 'updated_at',
                ])
            else:
                # UNCERTAIN is intentionally not made replayable: its provider
                # evidence stays fenced for explicit reconciliation.
                cls._finish(run, WebResearchRun.Status.FAILED)
            if unresolved_web_ai and not isinstance(
                exc,
                WebSearchOutcomeUncertain,
            ):
                raise WebSearchOutcomeUncertain(
                    'Результат AI-провайдера требует сверки; '
                    'локальное продолжение заблокировано.',
                ) from exc
            raise

    @classmethod
    @transaction.atomic
    def apply_ai_provider_operation(cls, operation_id) -> WebResearchRun:
        """Idempotently persist one exact paid research result and finish its run."""
        operation = (
            AIProviderOperation.objects.select_for_update()
            .select_related('tenant')
            .get(pk=operation_id)
        )
        if (
            operation.status != AIProviderOperation.Status.SETTLED
            or operation.task_type != AITaskType.WEB_RESEARCH
            or operation.domain_type
            != AIProviderOperation.DomainType.WEB_RESEARCH_RUN
        ):
            raise WebResearchUnavailable(
                'Операция не содержит завершённый результат исследования.',
            )
        try:
            run_id = int(operation.domain_reference)
        except (TypeError, ValueError) as exc:
            raise WebResearchUnavailable(
                'Некорректная ссылка операции на исследование.',
            ) from exc
        run = (
            WebResearchRun.objects.select_for_update()
            .select_related('tenant', 'product')
            .get(pk=run_id, tenant_id=operation.tenant_id)
        )
        if operation.apply_state == AIProviderOperation.ApplyState.APPLIED:
            return run
        if operation.apply_state != AIProviderOperation.ApplyState.PENDING:
            raise WebResearchUnavailable(
                'Результат исследования не ожидает применения.',
            )
        if not isinstance(operation.validated_result, dict):
            raise WebResearchUnavailable(
                'Durable AI-результат исследования отсутствует.',
            )

        # Revalidate persisted JSON before it reaches domain models. The paid
        # payload and its applied marker commit atomically with all claims.
        extracted = WebResearchAgent._parse_response(json.dumps(
            operation.validated_result,
            ensure_ascii=False,
            separators=(',', ':'),
        ))
        evidence = list(run.evidence.all())
        claims = cls._save_extracted_claims(run, extracted, evidence)
        offers = list(run.offers.all())
        run.ai_provider = operation.provider
        run.ai_model = operation.model_id
        run.claim_count = len(claims)
        run.offer_count = len(offers)
        run.coverage_after = enrichment_coverage(run.product)
        pending_claims = any(
            claim.review_status == WebResearchClaim.ReviewStatus.PENDING
            for claim in claims
        )
        pending_offers = any(
            offer.review_status == CompetitorOffer.ReviewStatus.PENDING
            for offer in offers
        )
        run.status = (
            WebResearchRun.Status.NEED_REVIEW
            if pending_claims or pending_offers
            else WebResearchRun.Status.COMPLETED
            if claims or offers
            else WebResearchRun.Status.NO_RESULTS
        )
        run.error_message = ''
        run.finished_at = now()
        run.save(update_fields=[
            'ai_provider', 'ai_model', 'claim_count', 'offer_count',
            'coverage_after', 'status', 'error_message', 'finished_at',
            'updated_at',
        ])
        operation.apply_state = AIProviderOperation.ApplyState.APPLIED
        operation.applied_at = now()
        operation.save(update_fields=['apply_state', 'applied_at', 'updated_at'])
        # enqueue_durable_task writes its dispatch row in this transaction;
        # generation cannot be lost between run completion and commit.
        cls._generate_if_unblocked(run)
        return run

    @staticmethod
    def _collect_evidence(
        run, workflow: WebSearchWorkflow,
    ) -> tuple[list[WebResearchEvidence], list[str]]:
        plan = workflow.input_snapshot
        slots = plan.get('slots') if isinstance(plan, dict) else None
        providers = plan.get('providers') if isinstance(plan, dict) else None
        if not isinstance(slots, list) or not isinstance(providers, list):
            raise WebResearchUnavailable('Неизменяемый план поиска повреждён.')
        evidence_payloads = []
        existing_urls = set(run.evidence.values_list('url', flat=True))
        providers_used = []
        rank = run.evidence.count()
        last_error = None
        any_success = False
        consumed_attempt_ids: set[int] = set()
        for slot_value in slots:
            if not isinstance(slot_value, dict):
                raise WebResearchUnavailable('Неизменяемый план поиска повреждён.')
            query = str(slot_value.get('query') or '')
            slot = str(slot_value.get('slot') or '')
            count = int(slot_value.get('count') or 0)
            context = _search_context_from_plan(slot_value.get('context'))
            if not query or not slot or count <= 0:
                raise WebResearchUnavailable('Неизменяемый план поиска повреждён.')
            results = []
            selected_provider = None
            for provider_index, provider_plan in enumerate(providers):
                provider_id = (
                    str(provider_plan.get('provider_id') or '').strip().lower()
                    if isinstance(provider_plan, dict) else ''
                )
                request_payload = {
                    'provider_id': provider_id,
                    'query': query,
                    'count': count,
                    'context': context.to_snapshot(),
                }
                request_fingerprint = fingerprint_web_search_request(request_payload)
                call_key = deterministic_web_search_call_key(
                    provider_id=provider_id,
                    call_kind='text',
                    slot=f'{slot}:provider:{provider_index}',
                )
                try:
                    replay = replay_recorded_web_search(
                        workflow,
                        call_key=call_key,
                        request_fingerprint=request_fingerprint,
                        restore_result=_web_search_results_from_checkpoint,
                    )
                    if replay is None:
                        provider, connection = WebResearchService._provider_from_plan(
                            run, provider_plan,
                        )
                        execution = execute_recorded_web_search(
                            workflow=workflow,
                            provider=provider,
                            connection=connection,
                            run=run,
                            query=query,
                            call_key=call_key,
                            request_fingerprint=request_fingerprint,
                            call_kind='text',
                            normalize_result=_web_search_results_to_checkpoint,
                            restore_result=_web_search_results_from_checkpoint,
                            call=partial(
                                WebResearchService._execute_provider_search,
                                provider,
                                query,
                                count,
                                context,
                            ),
                        )
                    else:
                        execution = replay
                    consumed_attempt_ids.add(execution.attempt_id)
                    candidate_results = execution.result
                    any_success = True
                except WebSearchProviderError as exc:
                    if exc.attempt_id is not None:
                        consumed_attempt_ids.add(exc.attempt_id)
                    last_error = exc
                    if exc.outcome_uncertain:
                        raise WebSearchOutcomeUncertain(
                            'Результат поискового провайдера неизвестен; '
                            'автоматический fallback запрещён.',
                        ) from exc
                    continue
                if candidate_results:
                    results = candidate_results
                    selected_provider = provider_id
                    if selected_provider not in providers_used:
                        providers_used.append(selected_provider)
                    break
            for result in results:
                if result.url in existing_urls or not is_safe_public_http_url(result.url):
                    continue
                combined_text = ' '.join([result.title, result.snippet, result.content])
                if not result_matches_context(result.url, combined_text, context):
                    continue
                domain = (urlparse(result.url).hostname or '').lower()
                if not domain:
                    continue
                rank += 1
                evidence_payloads.append({
                    'query': query[:500],
                    'rank': rank,
                    'provider_id': selected_provider or '',
                    'title': result.title[:500],
                    'url': result.url[:2000],
                    'domain': domain[:255],
                    'snippet': ' '.join(filter(None, [
                        result.snippet, result.content[:4000],
                    ]))[:6000],
                    'raw_content': (result.raw_content or result.content)[:50000],
                })
                existing_urls.add(result.url)
        if not any_success and last_error is not None:
            with transaction.atomic():
                type(run.tenant).objects.select_for_update().only('pk').get(
                    pk=run.tenant_id,
                )
                locked_run = WebResearchRun.objects.select_for_update().get(pk=run.pk)
                locked_run.error_message = str(last_error)[:2000]
                WebResearchService._finish(locked_run, WebResearchRun.Status.FAILED)
                if consumed_attempt_ids:
                    acknowledge_web_search_workflow(
                        workflow.pk,
                        consumed_attempt_ids=consumed_attempt_ids,
                    )
                else:
                    release_empty_web_search_workflow(workflow.pk)
            raise WebResearchTerminalSearchFailure(str(last_error)) from last_error
        # Evidence rows and checkpoint apply state commit together. A hard kill
        # before this block leaves the checkpoint active for a no-network retry.
        with transaction.atomic():
            type(run.tenant).objects.select_for_update().only('pk').get(
                pk=run.tenant_id,
            )
            locked_run = WebResearchRun.objects.select_for_update().get(pk=run.pk)
            for payload in evidence_payloads:
                payload = dict(payload)
                WebResearchEvidence.objects.get_or_create(
                    run=locked_run,
                    url=payload.pop('url'),
                    defaults=payload,
                )
            evidence = list(locked_run.evidence.all())
            locked_run.search_provider = providers_used[0] if providers_used else ''
            locked_run.result_count = len(evidence)
            locked_run.save(update_fields=[
                'search_provider', 'result_count', 'updated_at',
            ])
            if consumed_attempt_ids:
                acknowledge_web_search_workflow(
                    workflow.pk,
                    consumed_attempt_ids=consumed_attempt_ids,
                )
            else:
                release_empty_web_search_workflow(workflow.pk)
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
            fitment_payload: _FitmentPayload = {
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
                run, WebResearchClaim.ClaimType.FITMENT, fitment_payload,
                item.get('confidence'), item.get('evidence_ids'), evidence_by_id,
            )
            if claim:
                first_url = claim.evidence.order_by('rank').values_list('url', flat=True).first() or ''
                fitment, created = VehicleFitment.objects.get_or_create(
                    tenant=run.tenant,
                    product=run.product,
                    source_id=SOURCE_ID,
                    make=fitment_payload['make'],
                    model=fitment_payload['model'],
                    generation=fitment_payload['generation'],
                    modification=fitment_payload['modification'],
                    engine_code=fitment_payload['engine_code'],
                    power_hp=fitment_payload['power_hp'],
                    defaults={
                        'source_url': first_url,
                        'date_from': fitment_payload['date_from'],
                        'date_to': fitment_payload['date_to'],
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
            'offer_count', 'coverage_after', 'error_message', 'finished_at', 'updated_at',
        ])
        return run

    @staticmethod
    def _generate_if_unblocked(run):
        # Claims from open-web evidence always require review. Generating before
        # approval would silently omit them from the grounded description context.
        if (
            not run.generate_after
            or run.status not in {
                WebResearchRun.Status.COMPLETED,
                WebResearchRun.Status.NO_RESULTS,
                WebResearchRun.Status.SKIPPED,
            }
            or run.purpose == WebResearchRun.Purpose.PRICING
        ):
            return
        from apps.core.dispatch import enqueue_durable_task
        enqueue_durable_task(
            'apps.ai_agent.tasks.generate_description_task',
            args=[run.product_id],
            deduplication_key=f'web-research-run:{run.pk}:ai-description',
            max_run_attempts=4,
        )

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
        last_error: Exception | None = None
        for model in AIModelRouter.candidates(run.tenant, AITaskType.WEB_RESEARCH):
            estimated_input = max(1, (len(prompt) + len(message)) // 4)
            estimated_credits = model.estimate_credits(estimated_input, model.max_output_tokens)
            try:
                operation = begin_ai_provider_operation(
                    tenant=run.tenant,
                    task_type=AITaskType.WEB_RESEARCH,
                    provider=model.provider,
                    model_id=model.external_id,
                    reserved_amount=estimated_credits,
                    domain_type=AIProviderOperation.DomainType.WEB_RESEARCH_RUN,
                    domain_reference=str(run.pk),
                    reservation_details={
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
            mark_ai_provider_network_started(operation.pk)
            try:
                provider_result = call_model(
                    model, prompt, message, output_schema=WEB_RESEARCH_OUTPUT_SCHEMA,
                )
            except AIProviderError as exc:
                if not exc.request_not_accepted:
                    mark_ai_provider_operation_uncertain(
                        operation.pk,
                        error_code=exc.code,
                    )
                    self._log(
                        run, model, 'error', started,
                        error='provider_outcome_uncertain',
                    )
                    # Preserve the reservation until an operator can reconcile
                    # a request that the provider may already have billed.
                    raise WebResearchUnavailable(
                        'Результат AI-провайдера неизвестен; автоматический '
                        'fallback запрещён.',
                    ) from exc
                release_ai_provider_operation(
                    operation.pk,
                    reason='web_research_failed',
                )
                self._log(run, model, 'error', started, error=str(exc))
                last_error = exc
                continue
            except Exception as exc:
                mark_ai_provider_operation_uncertain(
                    operation.pk,
                    error_code='post_provider_failure',
                )
                self._log(
                    run, model, 'error', started,
                    error='provider_outcome_uncertain',
                )
                raise WebResearchUnavailable(
                    'Ошибка после начала AI-запроса; операция передана на '
                    'сверку, fallback запрещён.',
                ) from exc

            try:
                parsed = self._parse_response(provider_result.text)
            except WebResearchValidationError as exc:
                try:
                    actual = model.calculate_credits(
                        input_tokens=provider_result.input_tokens,
                        cached_input_tokens=provider_result.cached_input_tokens,
                        output_tokens=provider_result.output_tokens,
                    )
                    operation, charged = settle_ai_provider_operation(
                        operation.pk,
                        actual_amount=actual,
                        terminal_reason='validation_rejected',
                        details={
                            'task_type': AITaskType.WEB_RESEARCH,
                            'provider': model.provider,
                            'model': model.external_id,
                            'research_run_id': run.pk,
                            'validation_rejected': True,
                        },
                    )
                except Exception as settlement_exc:
                    mark_ai_provider_operation_uncertain(
                        operation.pk,
                        error_code='settlement_failed',
                    )
                    self._log(
                        run, model, 'error', started,
                        error='provider_settlement_uncertain',
                        input_tokens=provider_result.input_tokens,
                        cached_input_tokens=provider_result.cached_input_tokens,
                        output_tokens=provider_result.output_tokens,
                    )
                    raise WebResearchUnavailable(
                        'Не удалось подтвердить списание AI-кредитов; '
                        'операция передана на сверку.',
                    ) from settlement_exc
                self._log(
                    run, model, AIRequestLog.STATUS_REJECTED, started,
                    error=str(exc),
                    input_tokens=provider_result.input_tokens,
                    cached_input_tokens=provider_result.cached_input_tokens,
                    output_tokens=provider_result.output_tokens,
                    charged=charged,
                )
                last_error = exc
                continue
            except Exception as exc:
                mark_ai_provider_operation_uncertain(
                    operation.pk,
                    error_code='post_provider_failure',
                )
                self._log(
                    run, model, 'error', started,
                    error='provider_outcome_uncertain',
                )
                raise WebResearchUnavailable(
                    'Ошибка обработки ответа AI-провайдера; операция '
                    'передана на сверку, fallback запрещён.',
                ) from exc

            try:
                actual = model.calculate_credits(
                    input_tokens=provider_result.input_tokens,
                    cached_input_tokens=provider_result.cached_input_tokens,
                    output_tokens=provider_result.output_tokens,
                )
                operation, charged = settle_ai_provider_operation(
                    operation.pk,
                    actual_amount=actual,
                    details={
                        'task_type': AITaskType.WEB_RESEARCH,
                        'provider': model.provider,
                        'model': model.external_id,
                        'research_run_id': run.pk,
                    },
                    validated_result=parsed,
                    apply_required=True,
                )
            except Exception as exc:
                mark_ai_provider_operation_uncertain(
                    operation.pk,
                    error_code='settlement_failed',
                )
                self._log(
                    run, model, 'error', started,
                    error='provider_settlement_uncertain',
                    input_tokens=provider_result.input_tokens,
                    cached_input_tokens=provider_result.cached_input_tokens,
                    output_tokens=provider_result.output_tokens,
                )
                raise WebResearchUnavailable(
                    'Не удалось подтвердить списание AI-кредитов; '
                    'операция передана на сверку.',
                ) from exc
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
            result = dict(parsed)
            result['_provider_operation_id'] = str(operation.pk)
            return result, model
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
        offers = parsed.get('offers', [])
        if not isinstance(offers, list) or any(
            not isinstance(item, dict)
            or not WebResearchAgent._is_evidence_id_list(item.get('evidence_ids'))
            for item in offers
        ):
            raise WebResearchValidationError('AI вернул неверную структуру поля offers.')
        parsed['offers'] = offers
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
