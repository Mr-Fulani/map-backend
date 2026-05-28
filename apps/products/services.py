import hashlib
import json
import re
from decimal import Decimal

from django.utils.timezone import now

from apps.products.enrichment import make_value_hash, normalize_part_code
from apps.products.models import (
    GlobalPart, GlobalPartFitment, GlobalPartRelation,
    Product, ProductAttribute, ProductBulkActionJob, ProductCatalogClassification,
    ProductCrossCode, ProductEnrichmentFact, ProductParseJob, VehicleFitment,
    VehicleMake, VehicleModel,
)
from apps.products.part_parsers import ParsedPart, PartNotFound, get_part_parser
from apps.products.source_policy import (
    DEFAULT_PART_SOURCE, get_part_source_config, should_auto_apply_fitment,
    should_auto_apply_relation,
)


def _compute_hash(data: dict) -> str:
    """SHA256-хэш ключевых полей товара — используется для обнаружения изменений."""
    payload = {
        'name': data.get('name', ''),
        'brand': data.get('brand', ''),
        'price': str(data.get('price', '')),
        'stock_qty': data.get('stock_qty', 0),
        'category': data.get('category', ''),
        'condition': data.get('condition', 'new'),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class QuotaExceeded(Exception):
    """Превышен лимит AI-генераций для тенанта."""


class AutoPartsEnrichmentDisabled(Exception):
    """Автозапчастное обогащение отключено для домена каталога tenant-а."""


class ProductIsNotAutoPart(Exception):
    """Товар не похож на автозапчасть для смешанного каталога tenant-а."""


class ProductService:
    """Сервис управления товарами: создание/обновление из источников данных."""

    @staticmethod
    def upsert_from_source(tenant, datasource, data: dict) -> tuple[Product, str]:
        """
        Создаёт или обновляет товар из данных адаптера.

        Возвращает (product, status) где status: 'created' | 'updated' | 'unchanged'.
        Unchanged означает что данные не изменились — задача в Celery не нужна.
        """
        hash_new = _compute_hash(data)
        uuid_1c = data.get('uuid') or None

        lookup = {'tenant': tenant, 'datasource': datasource, 'article': data['article']}
        defaults = {
            'name': data.get('name', ''),
            'brand': data.get('brand', ''),
            'category_1c': data.get('category', ''),
            'condition': data.get('condition', Product.CONDITION_NEW),
            'price': Decimal(str(data.get('price', '0'))),
            'stock_qty': int(data.get('stock_qty', 0)),
            'warehouse': data.get('warehouse', ''),
            'description_1c': data.get('description', ''),
            'hash_1c': hash_new,
        }
        if uuid_1c is not None:
            defaults['uuid_1c'] = uuid_1c

        # Читаем старый хэш ДО update_or_create — иначе всегда будет 'unchanged'
        try:
            existing = Product.objects.get(**lookup)
            old_hash = existing.hash_1c
        except Product.DoesNotExist:
            existing = None
            old_hash = None

        product, created = Product.objects.update_or_create(**lookup, defaults=defaults)
        if created:
            return product, 'created'
        if old_hash != hash_new:
            return product, 'updated'
        return product, 'unchanged'

    @staticmethod
    def schedule_ai_generation(product, tenant) -> None:
        """
        Проверяет лимит AI-кредитов и ставит задачу генерации описания в очередь Celery.

        Raises:
            QuotaExceeded: превышен лимит AI-генераций тенанта.
        """
        from apps.billing.services import LimitChecker
        from django.db import transaction

        can, reason = LimitChecker().can_generate_ai(tenant)
        if not can:
            raise QuotaExceeded(reason)
        product_id = product.pk
        transaction.on_commit(lambda: _enqueue_ai_generation(product_id))

    @staticmethod
    def detect_change_type(old_data: dict, new_data: dict) -> str:
        """
        Определяет тип изменения товара.

        Нужно для решения: надо ли перегенерировать описание и как обновить листинг.
        Возвращает: 'price_only' | 'stock_only' | 'content' | 'category'
        """
        price_changed = str(old_data.get('price')) != str(new_data.get('price'))
        stock_changed = old_data.get('stock_qty') != new_data.get('stock_qty')
        category_changed = old_data.get('category') != new_data.get('category')

        content_fields = {'name', 'brand', 'condition', 'description'}
        content_changed = any(old_data.get(f) != new_data.get(f) for f in content_fields)

        if category_changed:
            return 'category'
        if content_changed:
            return 'content'
        if price_changed and not stock_changed:
            return 'price_only'
        if stock_changed and not price_changed:
            return 'stock_only'
        return 'content'


class ProductEnrichmentService:
    """Сервис tenant-scoped сохранения данных обогащения товара."""

    AUTO_PARTS_MARKERS = [
        'авто', 'автомоб', 'запчаст', 'oem', 'кросс',
        'тормоз', 'колод', 'диск торм', 'суппорт',
        'амортиз', 'стойк', 'подвес', 'рычаг', 'шаровая',
        'рулев', 'рейка', 'тяга', 'наконечник',
        'двигател', 'мотор', 'фильтр', 'свеч', 'ремень',
        'toyota', 'lexus', 'hyundai', 'kia', 'mercedes', 'benz',
        'bmw', 'audi', 'volkswagen', 'nissan', 'renault',
        'brembo', 'trw', 'kyb',
    ]

    @staticmethod
    def ensure_auto_parts_enabled(tenant) -> None:
        if not getattr(tenant, 'supports_auto_parts_enrichment', True):
            raise AutoPartsEnrichmentDisabled(
                'Автозапчастное обогащение доступно только для каталога автозапчастей.'
            )

    NON_AUTO_PARTS_MARKERS = [
        'кольц', 'серьг', 'браслет', 'цепоч', 'ожерель', 'подвеск',
        'украшен', 'ювелир', 'золот', 'серебр',
        'плать', 'рубаш', 'брюк', 'куртк', 'обув', 'одежд',
    ]

    @classmethod
    def classify_product_catalog_domain(
        cls, product: Product, save: bool = True,
    ) -> ProductCatalogClassification:
        text = ' '.join([
            product.name or '',
            product.category_1c or '',
            product.description_1c or '',
            product.brand or '',
        ]).lower()
        auto_matches = [marker for marker in cls.AUTO_PARTS_MARKERS if marker in text]
        non_auto_matches = [marker for marker in cls.NON_AUTO_PARTS_MARKERS if marker in text]

        if auto_matches:
            domain = ProductCatalogClassification.Domain.AUTO_PARTS
            confidence = 0.9 if len(auto_matches) > 1 else 0.75
            reason = f'Найдены признаки автозапчасти: {", ".join(auto_matches[:5])}.'
            needs_review = confidence < 0.8
        elif non_auto_matches:
            domain = ProductCatalogClassification.Domain.JEWELLERY
            confidence = 0.85
            reason = f'Найдены признаки неавтомобильного товара: {", ".join(non_auto_matches[:5])}.'
            needs_review = False
        else:
            domain = ProductCatalogClassification.Domain.UNKNOWN
            confidence = 0.3
            reason = 'Не найдено достаточно признаков автозапчасти или другого домена.'
            needs_review = True

        defaults = {
            'domain': domain,
            'confidence': confidence,
            'source': ProductCatalogClassification.Source.RULES,
            'reason': reason,
            'needs_review': needs_review,
        }
        if save:
            classification, _ = ProductCatalogClassification.objects.update_or_create(
                tenant=product.tenant,
                product=product,
                defaults=defaults,
            )
            return classification
        return ProductCatalogClassification(
            tenant=product.tenant,
            product=product,
            **defaults,
        )

    @classmethod
    def get_or_classify_product_catalog_domain(cls, product: Product) -> ProductCatalogClassification:
        try:
            return product.catalog_classification
        except ProductCatalogClassification.DoesNotExist:
            return cls.classify_product_catalog_domain(product)

    @classmethod
    def is_product_auto_part_candidate(cls, product: Product) -> bool:
        classification = cls.get_or_classify_product_catalog_domain(product)
        return (
            classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
            and classification.confidence >= 0.7
        )

    @classmethod
    def ensure_product_auto_parts_eligible(cls, tenant, product: Product | None) -> None:
        cls.ensure_auto_parts_enabled(tenant)
        if product is None and getattr(tenant, 'requires_product_auto_parts_check', False):
            raise ProductIsNotAutoPart(
                'Для смешанного каталога нужно указать товар, чтобы проверить, что это автозапчасть.'
            )
        if (
            product is not None
            and getattr(tenant, 'requires_product_auto_parts_check', False)
            and not cls.is_product_auto_part_candidate(product)
        ):
            raise ProductIsNotAutoPart(
                'Товар не похож на автозапчасть, поэтому parser не запускается для смешанного каталога.'
            )

    @staticmethod
    def _ensure_product_tenant(product: Product, tenant) -> None:
        if product.tenant_id != tenant.id:
            raise ValueError('Product tenant mismatch')

    @classmethod
    def create_attribute(cls, tenant, product: Product, **data) -> ProductAttribute:
        cls._ensure_product_tenant(product, tenant)
        value = data.get('value', '')
        data.setdefault('value_hash', make_value_hash(value))
        return ProductAttribute.objects.create(tenant=tenant, product=product, **data)

    @classmethod
    def create_cross_code(cls, tenant, product: Product, **data) -> ProductCrossCode:
        cls._ensure_product_tenant(product, tenant)
        return ProductCrossCode.objects.create(tenant=tenant, product=product, **data)

    @classmethod
    def create_fitment(cls, tenant, product: Product, **data) -> VehicleFitment:
        cls._ensure_product_tenant(product, tenant)
        return VehicleFitment.objects.create(tenant=tenant, product=product, **data)

    @classmethod
    def create_fact(cls, tenant, product: Product, **data) -> ProductEnrichmentFact:
        cls._ensure_product_tenant(product, tenant)
        value = data.get('value', '')
        data.setdefault('value_hash', make_value_hash(value))
        return ProductEnrichmentFact.objects.create(tenant=tenant, product=product, **data)

    @classmethod
    def create_parse_job(
        cls, tenant, product: Product | None, brand: str, article: str,
        normalized_article: str, source_id: str = DEFAULT_PART_SOURCE,
    ) -> ProductParseJob:
        cls.ensure_product_auto_parts_eligible(tenant, product)
        if product is not None:
            cls._ensure_product_tenant(product, tenant)
        return ProductParseJob.objects.create(
            tenant=tenant,
            product=product,
            brand=brand,
            article=article,
            normalized_article=normalized_article,
            source_id=source_id,
        )

    @classmethod
    def save_parsed_part(
        cls, tenant, product: Product, parsed: ParsedPart, source_id: str = DEFAULT_PART_SOURCE,
    ) -> None:
        """Сохраняет enrichment-данные, не трогая цену, остаток и склад."""
        from django.db import transaction

        cls._ensure_product_tenant(product, tenant)
        with transaction.atomic():
            ProductAttribute.objects.bulk_create([
                ProductAttribute(
                    tenant=tenant,
                    product=product,
                    source_id=source_id,
                    name=name[:150],
                    raw_name=name[:150],
                    value=value,
                    value_hash=make_value_hash(value),
                )
                for name, value in parsed.attributes.items()
                if name and value
            ], ignore_conflicts=True)

            cross_objects = [
                ProductCrossCode(
                    tenant=tenant,
                    product=product,
                    source_id=source_id,
                    manufacturer=cross.manufacturer[:100],
                    code=cross.code[:100],
                    normalized_code=normalize_cross_code(cross.code),
                    code_type=cross.code_type,
                )
                for cross in parsed.cross_codes
                if normalize_cross_code(cross.code)
            ]
            ProductCrossCode.objects.bulk_create(cross_objects, ignore_conflicts=True)

            VehicleFitment.objects.bulk_create([
                VehicleFitment(
                    tenant=tenant,
                    product=product,
                    source_id=source_id,
                    make=fitment.make[:100],
                    model=fitment.model[:150],
                    generation=fitment.generation[:100],
                    date_from=fitment.date_from[:20],
                    date_to=fitment.date_to[:20],
                    modification=fitment.modification[:255],
                    engine_code=fitment.engine_code[:100],
                    power_hp=fitment.power_hp,
                    raw_text=fitment.raw_text,
                    confidence=fitment.confidence,
                    needs_review=fitment.needs_review,
                )
                for fitment in parsed.fitments
                if fitment.model
            ], ignore_conflicts=True)

            description_facts = [
                ProductEnrichmentFact(
                    tenant=tenant,
                    product=product,
                    source_id=source_id,
                    fact_type=ProductEnrichmentFact.FactType.DESCRIPTION_HINT,
                    name=name[:150],
                    value=value,
                    value_hash=make_value_hash(value),
                )
                for name, value in parsed.description_facts.items()
                if name and value
            ]
            ProductEnrichmentFact.objects.bulk_create(description_facts, ignore_conflicts=True)

            cls.refresh_product_denormalized_enrichment(product)
            product.save(update_fields=['oem_numbers', 'cross_numbers', 'applicability'])
            ProductKnowledgeGraphService.learn_from_parsed_part(
                product=product,
                parsed=parsed,
                source_id=source_id,
            )

    @staticmethod
    def refresh_product_denormalized_enrichment(product: Product) -> None:
        oem_numbers = []
        cross_numbers = []
        for cross in product.cross_codes.order_by('source_id', 'manufacturer', 'code'):
            target = oem_numbers if cross.code_type == ProductCrossCode.CodeType.OEM else cross_numbers
            if cross.normalized_code and cross.normalized_code not in target:
                target.append(cross.normalized_code)

        applicability = []
        seen_fitments = set()
        for fitment in product.fitments.order_by('source_id', 'make', 'model', 'generation'):
            key = (
                fitment.make, fitment.model, fitment.generation,
                fitment.modification, fitment.engine_code, fitment.power_hp,
            )
            if not fitment.model or key in seen_fitments:
                continue
            seen_fitments.add(key)
            applicability.append({
                'make': fitment.make,
                'model': fitment.model,
                'generation': fitment.generation,
                'date_from': fitment.date_from,
                'date_to': fitment.date_to,
                'modification': fitment.modification,
                'engine_code': fitment.engine_code,
                'power_hp': fitment.power_hp,
                'source_id': fitment.source_id,
            })

        product.oem_numbers = oem_numbers[:50]
        product.cross_numbers = cross_numbers[:50]
        product.applicability = applicability[:500]

    @classmethod
    def run_parse_job(cls, job_id: int) -> dict:
        job = ProductParseJob.objects.select_related('tenant', 'product').get(pk=job_id)
        job.status = ProductParseJob.Status.RUNNING
        job.started_at = now()
        job.error_message = ''
        job.save(update_fields=['status', 'started_at', 'error_message'])

        try:
            product = job.product or cls._find_single_product_for_job(job)
            applied_knowledge = ProductKnowledgeGraphService.apply_known_knowledge_to_product(product)
            if applied_knowledge['relations_count'] or applied_knowledge['fitments_count']:
                job.product = product
                job.source_url = ''
                job.parsed_data = {
                    'applied_from': 'knowledge_graph',
                    **applied_knowledge,
                }
                status = (
                    ProductParseJob.Status.SUCCESS
                    if applied_knowledge['fitments_count']
                    else ProductParseJob.Status.NEED_REVIEW
                )
                cls._finish_job(job, status)
                return {
                    'job_id': job_id,
                    'product_id': product.pk,
                    'status': status,
                    'source_id': job.source_id,
                    'image_urls': [],
                    **applied_knowledge,
                }

            parser = get_part_parser(job.source_id)
            try:
                html, source_url = parser.fetch(job.brand, job.article)
                parsed = parser.parse_html(html, job.brand, job.article, source_url=source_url)
            except PartNotFound:
                if not hasattr(parser, 'fetch_search'):
                    raise
                html, source_url = parser.fetch_search(job.article)
                parsed = parser.parse_search_html(
                    html, job.brand, job.article, source_url=source_url,
                )
            cls.save_parsed_part(job.tenant, product, parsed, source_id=job.source_id)
        except PartNotFound as exc:
            cls._finish_job(job, ProductParseJob.Status.NOT_FOUND, error_message=str(exc))
            return {
                'job_id': job_id,
                'product_id': product.pk if product else None,
                'status': ProductParseJob.Status.NOT_FOUND,
                'source_id': job.source_id,
                'image_urls': [],
            }
        except Exception as exc:
            cls._finish_job(job, ProductParseJob.Status.FAILED, error_message=str(exc))
            raise

        status = (
            ProductParseJob.Status.SUCCESS
            if parsed.fitments else ProductParseJob.Status.NEED_REVIEW
        )
        job.product = product
        job.source_url = source_url
        job.raw_html = html[:5_000_000]
        job.raw_text = parsed.raw_text
        job.parsed_data = parsed.to_dict()
        cls._finish_job(job, status)
        return {
            'job_id': job_id,
            'product_id': product.pk,
            'status': status,
            'source_id': job.source_id,
            'image_urls': parsed.image_urls,
        }

    @classmethod
    def _find_single_product_for_job(cls, job: ProductParseJob) -> Product:
        qs = Product.objects.filter(tenant=job.tenant, article__iexact=job.article)
        if job.brand:
            qs = qs.filter(brand__iexact=job.brand)
        count = qs.count()
        if count != 1:
            raise ValueError(
                f'Expected exactly one product for enrichment job, found {count}'
            )
        return qs.get()

    @staticmethod
    def _finish_job(
        job: ProductParseJob, status: str, error_message: str = '',
    ) -> None:
        job.status = status
        job.error_message = error_message
        job.finished_at = now()
        job.save(update_fields=[
            'product', 'source_url', 'status', 'error_message', 'raw_html',
            'raw_text', 'parsed_data', 'finished_at',
        ])


class ProductBulkActionService:
    """Сервис throttled массовых действий по tenant-scoped товарам."""

    ALLOWED_ACTIONS = {
        ProductBulkActionJob.Action.ENRICH_SELECTED,
        ProductBulkActionJob.Action.ENRICH_MISSING_DATA,
        ProductBulkActionJob.Action.GENERATE_DESCRIPTIONS,
        ProductBulkActionJob.Action.ENRICH_THEN_GENERATE,
        ProductBulkActionJob.Action.FIND_IMAGES,
    }

    @staticmethod
    def create_job(
        tenant, action: str, product_ids: list[int], source_id: str = DEFAULT_PART_SOURCE,
        batch_size: int = 20, pause_seconds: int = 60,
    ) -> ProductBulkActionJob:
        if action not in ProductBulkActionService.ALLOWED_ACTIONS:
            raise ValueError('Unknown bulk action')
        if action in [
            ProductBulkActionJob.Action.ENRICH_SELECTED,
            ProductBulkActionJob.Action.ENRICH_MISSING_DATA,
            ProductBulkActionJob.Action.ENRICH_THEN_GENERATE,
        ]:
            ProductEnrichmentService.ensure_auto_parts_enabled(tenant)
        source_config = get_part_source_config(source_id)
        products = list(Product.objects.filter(tenant=tenant, pk__in=product_ids).order_by('pk'))
        if action in [
            ProductBulkActionJob.Action.ENRICH_SELECTED,
            ProductBulkActionJob.Action.ENRICH_MISSING_DATA,
            ProductBulkActionJob.Action.ENRICH_THEN_GENERATE,
        ] and getattr(tenant, 'requires_product_auto_parts_check', False):
            products = [
                product for product in products
                if ProductEnrichmentService.is_product_auto_part_candidate(product)
            ]
        valid_ids = [product.pk for product in products]
        skipped_count = max(len(set(product_ids)) - len(valid_ids), 0)
        return ProductBulkActionJob.objects.create(
            tenant=tenant,
            action=action,
            source_id=source_id,
            product_ids=valid_ids,
            total_count=len(valid_ids),
            skipped_count=skipped_count,
            batch_size=max(1, min(batch_size or source_config['batch_size'], source_config['batch_size'])),
            pause_seconds=max(
                source_config['min_pause_seconds'],
                pause_seconds or source_config['default_pause_seconds'],
            ),
        )

    @staticmethod
    def process_next_batch(job_id: int) -> dict:
        from django.db import transaction

        with transaction.atomic():
            job = ProductBulkActionJob.objects.select_for_update().get(pk=job_id)
            if job.status in [
                ProductBulkActionJob.Status.PAUSED,
                ProductBulkActionJob.Status.CANCELLED,
                ProductBulkActionJob.Status.SUCCESS,
                ProductBulkActionJob.Status.FAILED,
            ]:
                return {'job_id': job_id, 'status': job.status}

            if job.started_at is None:
                job.started_at = now()
            job.status = ProductBulkActionJob.Status.RUNNING
            remaining_ids = job.product_ids[job.processed_count:]
            batch_ids = remaining_ids[:job.batch_size]

            if not batch_ids:
                job.status = ProductBulkActionJob.Status.SUCCESS
                job.finished_at = now()
                job.next_batch_at = None
                job.save(update_fields=['status', 'finished_at', 'next_batch_at', 'updated_at'])
                return {'job_id': job_id, 'status': job.status}

            products = Product.objects.filter(
                tenant=job.tenant, pk__in=batch_ids,
            ).select_related('tenant')
            product_map = {product.pk: product for product in products}

            queued = 0
            for product_id in batch_ids:
                product = product_map.get(product_id)
                if product is None:
                    job.skipped_count += 1
                    continue
                if job.action in [
                    ProductBulkActionJob.Action.ENRICH_SELECTED,
                    ProductBulkActionJob.Action.ENRICH_MISSING_DATA,
                    ProductBulkActionJob.Action.ENRICH_THEN_GENERATE,
                ]:
                    try:
                        parse_job = ProductEnrichmentService.create_parse_job(
                            tenant=job.tenant,
                            product=product,
                            brand=product.brand,
                            article=product.article,
                            normalized_article=normalize_cross_code(product.article),
                            source_id=job.source_id,
                        )
                    except ProductIsNotAutoPart:
                        job.skipped_count += 1
                        continue
                    from apps.products.tasks import (
                        parse_single_part, parse_single_part_then_generate_description,
                    )
                    task = (
                        parse_single_part_then_generate_description
                        if job.action == ProductBulkActionJob.Action.ENRICH_THEN_GENERATE
                        else parse_single_part
                    )
                    transaction.on_commit(lambda pk=parse_job.pk, celery_task=task: celery_task.delay(pk))
                    queued += 1
                else:
                    job.skipped_count += 1

            job.processed_count += len(batch_ids)
            job.queued_count += queued
            if job.processed_count >= job.total_count:
                job.status = ProductBulkActionJob.Status.SUCCESS
                job.finished_at = now()
                job.next_batch_at = None
            else:
                from datetime import timedelta
                job.status = ProductBulkActionJob.Status.COOLING_DOWN
                job.next_batch_at = now() + timedelta(seconds=job.pause_seconds)
                from apps.products.tasks import process_bulk_product_action
                transaction.on_commit(
                    lambda: process_bulk_product_action.apply_async(
                        args=[job.pk], countdown=job.pause_seconds,
                    )
                )
            job.save(update_fields=[
                'status', 'started_at', 'processed_count', 'queued_count',
                'skipped_count', 'finished_at', 'next_batch_at', 'updated_at',
            ])
            return {
                'job_id': job_id,
                'status': job.status,
                'processed_count': job.processed_count,
                'queued_count': job.queued_count,
            }


def normalize_cross_code(code: str) -> str:
    return normalize_part_code(code)


class VehicleKnowledgeService:
    """Нормализует марки и модели авто для platform-level справочника."""

    MAKE_ALIASES = {
        'MB': 'MERCEDESBENZ',
        'MERCEDES': 'MERCEDESBENZ',
        'MERCEDESBENZ': 'MERCEDESBENZ',
        'MERCEDES BENZ': 'MERCEDESBENZ',
        'MERCEDES-BENZ': 'MERCEDESBENZ',
        'HYUNDAI KIA': 'HYUNDAIKIA',
        'HYUNDAI/KIA': 'HYUNDAIKIA',
        'HYUNDAI / KIA': 'HYUNDAIKIA',
        'TOYOTA LEXUS': 'TOYOTALEXUS',
        'TOYOTA-LEXUS': 'TOYOTALEXUS',
    }
    MAKE_DISPLAY_NAMES = {
        'HYUNDAIKIA': 'HYUNDAI / KIA',
        'MERCEDESBENZ': 'MERCEDES-BENZ',
        'TOYOTALEXUS': 'TOYOTA-LEXUS',
    }

    @classmethod
    def normalize_name(cls, value: str) -> str:
        return re.sub(r'[^A-Z0-9]+', '', (value or '').upper())

    @classmethod
    def normalize_make(cls, value: str) -> str:
        raw = (value or '').strip().upper()
        return cls.MAKE_ALIASES.get(raw, cls.normalize_name(raw))

    @classmethod
    def normalize_model(cls, value: str) -> str:
        return cls.normalize_name(value)

    @classmethod
    def upsert_make(cls, name: str) -> VehicleMake | None:
        normalized = cls.normalize_make(name)
        if not normalized:
            return None
        display_name = cls.MAKE_DISPLAY_NAMES.get(normalized, name.strip().upper())
        make, created = VehicleMake.objects.get_or_create(
            normalized_name=normalized,
            defaults={'name': display_name[:100], 'aliases': [name.strip()] if name.strip() else []},
        )
        if not created:
            cls._append_alias(make, name)
        return make

    @classmethod
    def upsert_model(cls, make: VehicleMake, name: str) -> VehicleModel | None:
        normalized = cls.normalize_model(name)
        if not normalized:
            return None
        model, created = VehicleModel.objects.get_or_create(
            make=make,
            normalized_name=normalized,
            defaults={'name': name.strip()[:150], 'aliases': [name.strip()] if name.strip() else []},
        )
        if not created:
            cls._append_alias(model, name)
        return model

    @classmethod
    def resolve_fitment(cls, fitment) -> tuple[VehicleMake | None, VehicleModel | None]:
        make = cls.upsert_make(fitment.make)
        if make is None:
            return None, None
        return make, cls.upsert_model(make, fitment.model)

    @staticmethod
    def _append_alias(instance, alias: str) -> None:
        alias = (alias or '').strip()
        if not alias or alias in instance.aliases:
            return
        instance.aliases = [*instance.aliases, alias][:50]
        instance.save(update_fields=['aliases', 'updated_at'])


class ProductKnowledgeGraphService:
    """Platform-level граф артикулов: OEM, аналоги, заменители и trade-связи."""

    CROSS_TO_RELATION = {
        ProductCrossCode.CodeType.OEM: GlobalPartRelation.RelationType.OEM,
        ProductCrossCode.CodeType.CROSS: GlobalPartRelation.RelationType.CROSS,
        ProductCrossCode.CodeType.TRADE: GlobalPartRelation.RelationType.TRADE,
        ProductCrossCode.CodeType.UNKNOWN: GlobalPartRelation.RelationType.UNKNOWN,
    }

    @staticmethod
    def normalize_brand(brand: str) -> str:
        return normalize_part_code(brand)

    @classmethod
    def upsert_part(
        cls, brand: str, article: str, title: str = '', source_id: str = '',
        source_url: str = '', confidence: float = 1.0, needs_review: bool = False,
    ) -> GlobalPart:
        normalized_brand = cls.normalize_brand(brand)
        normalized_article = normalize_part_code(article)
        if not normalized_article:
            raise ValueError('Global part article is required')

        part, created = GlobalPart.objects.get_or_create(
            normalized_brand=normalized_brand,
            normalized_article=normalized_article,
            defaults={
                'brand': brand[:100],
                'article': article[:100],
                'title': title[:500],
                'source_id': source_id[:50],
                'source_url': source_url,
                'confidence': confidence,
                'needs_review': needs_review,
                'last_seen_at': now(),
            },
        )
        if not created:
            update_fields = ['last_seen_at', 'updated_at']
            part.last_seen_at = now()
            if title and not part.title:
                part.title = title[:500]
                update_fields.append('title')
            if source_id and not part.source_id:
                part.source_id = source_id[:50]
                update_fields.append('source_id')
            if source_url and not part.source_url:
                part.source_url = source_url
                update_fields.append('source_url')
            part.needs_review = part.needs_review or needs_review
            if needs_review:
                update_fields.append('needs_review')
            part.confidence = max(part.confidence, confidence)
            update_fields.append('confidence')
            part.save(update_fields=sorted(set(update_fields)))
        return part

    @classmethod
    def upsert_relation(
        cls, source_part: GlobalPart, target_part: GlobalPart, relation_type: str,
        source_id: str = '', source_url: str = '', raw_text: str = '',
        confidence: float = 1.0, needs_review: bool = False,
    ) -> GlobalPartRelation:
        relation, created = GlobalPartRelation.objects.get_or_create(
            source_part=source_part,
            target_part=target_part,
            relation_type=relation_type,
            source_id=source_id[:50],
            defaults={
                'source_url': source_url,
                'raw_text': raw_text,
                'confidence': confidence,
                'needs_review': needs_review,
                'last_seen_at': now(),
            },
        )
        if not created:
            relation.last_seen_at = now()
            relation.confidence = max(relation.confidence, confidence)
            relation.needs_review = relation.needs_review or needs_review
            if source_url and not relation.source_url:
                relation.source_url = source_url
            if raw_text and not relation.raw_text:
                relation.raw_text = raw_text
            relation.save(update_fields=[
                'last_seen_at', 'confidence', 'needs_review',
                'source_url', 'raw_text', 'updated_at',
            ])
        return relation

    @classmethod
    def upsert_fitment(
        cls, part: GlobalPart, fitment, source_id: str = '',
        source_url: str = '',
    ) -> GlobalPartFitment:
        vehicle_make, vehicle_model = VehicleKnowledgeService.resolve_fitment(fitment)
        global_fitment, created = GlobalPartFitment.objects.get_or_create(
            part=part,
            source_id=source_id[:50],
            make=fitment.make[:100],
            model=fitment.model[:150],
            generation=fitment.generation[:100],
            modification=fitment.modification[:255],
            engine_code=fitment.engine_code[:100],
            power_hp=fitment.power_hp,
            defaults={
                'vehicle_make': vehicle_make,
                'vehicle_model': vehicle_model,
                'date_from': fitment.date_from[:20],
                'date_to': fitment.date_to[:20],
                'source_url': source_url,
                'raw_text': fitment.raw_text,
                'confidence': fitment.confidence,
                'needs_review': fitment.needs_review,
                'last_seen_at': now(),
            },
        )
        if not created:
            global_fitment.last_seen_at = now()
            global_fitment.confidence = max(global_fitment.confidence, fitment.confidence)
            global_fitment.needs_review = global_fitment.needs_review or fitment.needs_review
            if fitment.date_from and not global_fitment.date_from:
                global_fitment.date_from = fitment.date_from[:20]
            if fitment.date_to and not global_fitment.date_to:
                global_fitment.date_to = fitment.date_to[:20]
            if source_url and not global_fitment.source_url:
                global_fitment.source_url = source_url
            if fitment.raw_text and not global_fitment.raw_text:
                global_fitment.raw_text = fitment.raw_text
            if vehicle_make and global_fitment.vehicle_make_id is None:
                global_fitment.vehicle_make = vehicle_make
            if vehicle_model and global_fitment.vehicle_model_id is None:
                global_fitment.vehicle_model = vehicle_model
            global_fitment.save(update_fields=[
                'last_seen_at', 'confidence', 'needs_review', 'date_from',
                'date_to', 'source_url', 'raw_text', 'vehicle_make',
                'vehicle_model', 'updated_at',
            ])
        return global_fitment

    @classmethod
    def learn_from_parsed_part(
        cls, product: Product, parsed: ParsedPart, source_id: str = DEFAULT_PART_SOURCE,
    ) -> None:
        source_part = cls.upsert_part(
            brand=product.brand or parsed.brand,
            article=product.article or parsed.article,
            title=parsed.title,
            source_id=source_id,
            source_url=parsed.source_url,
        )
        for cross in parsed.cross_codes:
            normalized = normalize_part_code(cross.code)
            if not normalized:
                continue
            target_part = cls.upsert_part(
                brand=cross.manufacturer,
                article=cross.code,
                source_id=source_id,
                source_url=parsed.source_url,
            )
            relation_type = cls.CROSS_TO_RELATION.get(
                cross.code_type,
                GlobalPartRelation.RelationType.UNKNOWN,
            )
            cls.upsert_relation(
                source_part=source_part,
                target_part=target_part,
                relation_type=relation_type,
                source_id=source_id,
                source_url=parsed.source_url,
                raw_text=f'{cross.manufacturer}: {cross.code}'.strip(': '),
                confidence=0.8 if cross.code_type == ProductCrossCode.CodeType.UNKNOWN else 1.0,
                needs_review=cross.code_type == ProductCrossCode.CodeType.UNKNOWN,
            )
        for related in parsed.related_parts:
            if not normalize_part_code(related.article):
                continue
            target_part = cls.upsert_part(
                brand=related.brand,
                article=related.article,
                title=related.title,
                source_id=source_id,
                source_url=parsed.source_url,
                confidence=related.confidence,
                needs_review=related.needs_review,
            )
            cls.upsert_relation(
                source_part=source_part,
                target_part=target_part,
                relation_type=related.relation_type,
                source_id=source_id,
                source_url=parsed.source_url,
                raw_text=related.raw_text,
                confidence=related.confidence,
                needs_review=related.needs_review,
            )
        for fitment in parsed.fitments:
            if not fitment.model:
                continue
            cls.upsert_fitment(
                part=source_part,
                fitment=fitment,
                source_id=source_id,
                source_url=parsed.source_url,
            )

    @classmethod
    def apply_known_relations_to_product(cls, product: Product) -> int:
        source_part = GlobalPart.objects.filter(
            normalized_brand=cls.normalize_brand(product.brand),
            normalized_article=normalize_part_code(product.article),
        ).first()
        if source_part is None:
            return 0

        created = 0
        for relation in source_part.outgoing_relations.select_related('target_part'):
            if not should_auto_apply_relation(relation):
                continue
            code_type = cls._relation_to_cross_code_type(relation.relation_type)
            _, was_created = ProductCrossCode.objects.get_or_create(
                tenant=product.tenant,
                product=product,
                source_id=relation.source_id or 'knowledge_graph',
                manufacturer=relation.target_part.brand[:100],
                normalized_code=relation.target_part.normalized_article,
                code_type=code_type,
                defaults={'code': relation.target_part.article[:100]},
            )
            if was_created:
                created += 1

        if created:
            ProductEnrichmentService.refresh_product_denormalized_enrichment(product)
            product.save(update_fields=['oem_numbers', 'cross_numbers', 'applicability'])
        return created

    @classmethod
    def apply_known_fitments_to_product(cls, product: Product) -> int:
        source_part = GlobalPart.objects.filter(
            normalized_brand=cls.normalize_brand(product.brand),
            normalized_article=normalize_part_code(product.article),
        ).first()
        if source_part is None:
            return 0

        created = 0
        fitments = source_part.fitments.order_by('make', 'model', 'generation')
        for fitment in fitments:
            if not should_auto_apply_fitment(fitment):
                continue
            _, was_created = VehicleFitment.objects.get_or_create(
                tenant=product.tenant,
                product=product,
                source_id=fitment.source_id or 'knowledge_graph',
                make=fitment.make[:100],
                model=fitment.model[:150],
                generation=fitment.generation[:100],
                modification=fitment.modification[:255],
                engine_code=fitment.engine_code[:100],
                power_hp=fitment.power_hp,
                defaults={
                    'date_from': fitment.date_from[:20],
                    'date_to': fitment.date_to[:20],
                    'raw_text': fitment.raw_text,
                    'confidence': fitment.confidence,
                    'needs_review': False,
                },
            )
            if was_created:
                created += 1

        if created:
            ProductEnrichmentService.refresh_product_denormalized_enrichment(product)
            product.save(update_fields=['oem_numbers', 'cross_numbers', 'applicability'])
        return created

    @classmethod
    def apply_known_knowledge_to_product(cls, product: Product) -> dict:
        relations_count = cls.apply_known_relations_to_product(product)
        fitments_count = cls.apply_known_fitments_to_product(product)
        return {
            'relations_count': relations_count,
            'fitments_count': fitments_count,
        }

    @classmethod
    def _relation_to_cross_code_type(cls, relation_type: str) -> str:
        if relation_type == GlobalPartRelation.RelationType.OEM:
            return ProductCrossCode.CodeType.OEM
        if relation_type == GlobalPartRelation.RelationType.TRADE:
            return ProductCrossCode.CodeType.TRADE
        if relation_type in [
            GlobalPartRelation.RelationType.CROSS,
            GlobalPartRelation.RelationType.ANALOGUE,
            GlobalPartRelation.RelationType.REPLACEMENT,
        ]:
            return ProductCrossCode.CodeType.CROSS
        return ProductCrossCode.CodeType.UNKNOWN


def _enqueue_ai_generation(product_id: int) -> None:
    """Ставит задачу генерации AI-описания в Celery."""
    from apps.ai_agent.tasks import generate_description_task
    generate_description_task.delay(product_id)
