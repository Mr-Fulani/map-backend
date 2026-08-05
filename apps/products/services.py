import hashlib
import json
import re
from decimal import Decimal

from django.utils.timezone import now

from apps.products.enrichment import make_value_hash, normalize_part_code
from apps.products.catalog_category_seed import BASE_CATEGORY_TEMPLATE_TREE
from apps.products.models import (
    GlobalPart, GlobalPartFitment, GlobalPartRelation, PartCategory,
    Product, ProductAttribute, ProductBulkActionJob, ProductCatalogClassification,
    ProductCrossCode, ProductEnrichmentFact, ProductParseJob, ReviewStatus, VehicleFitment,
    TenantCatalogCategory, TenantCategoryMapping, ProductBrand, ProductBrandAlias,
    VehicleGeneration, VehicleMake, VehicleModel,
)
from apps.products.part_category_seed import (
    BASE_PART_CATEGORY_TREE, normalize_category_name,
)
from apps.products.part_parsers import ParsedPart, PartNotFound, get_part_parser
from apps.products.source_policy import (
    DEFAULT_PART_SOURCE, can_raise_confidence, get_part_source_config, has_conflicting_fact,
    has_conflicting_fitment, should_auto_apply_fitment, should_auto_apply_relation,
    should_mark_needs_review,
)
from apps.tenants.models import CatalogDomain, TenantCatalogDomain


# Латинские буквы-двойники → кириллица. В источниках (1С/CSV) названия часто
# приходят с подменой: «Oпopa шapoвaя» = латинские O/o/p/a. Для матчинга категорий
# приводим к кириллице (на отображение товара не влияет).
_HOMOGLYPHS = str.maketrans({
    'A': 'А', 'B': 'В', 'C': 'С', 'E': 'Е', 'H': 'Н', 'K': 'К', 'M': 'М',
    'O': 'О', 'P': 'Р', 'T': 'Т', 'X': 'Х', 'Y': 'У',
    'a': 'а', 'c': 'с', 'e': 'е', 'k': 'к', 'm': 'м', 'o': 'о', 'p': 'р',
    'x': 'х', 'y': 'у',
})


def dehomoglyph(text: str) -> str:
    """Приводит латинские буквы-двойники к кириллице (для матчинга категорий)."""
    return (text or '').translate(_HOMOGLYPHS)


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


class ProductCategorySeedService:
    """Seeds base category templates for the platform and tenant catalogs."""

    SEED_SOURCE = 'platform_auto_parts_seed'

    @staticmethod
    def _merge_aliases(category, aliases: list[str]) -> bool:
        merged = list(category.aliases)
        changed = False
        for alias in aliases:
            if alias and alias not in merged:
                merged.append(alias)
                changed = True
        if changed:
            category.aliases = merged
            category.save(update_fields=['aliases', 'updated_at'])
        return changed

    @classmethod
    def seed_platform_categories(cls) -> int:
        created_count = 0
        for root in BASE_PART_CATEGORY_TREE:
            category, created = PartCategory.objects.update_or_create(
                normalized_name=normalize_category_name(root['name']),
                defaults={
                    'name': root['name'],
                    'parent': None,
                    'aliases': root.get('aliases', []),
                    'fitment_required': root.get('fitment_required', True),
                },
            )
            created_count += int(created)
            for child_name, aliases, fitment_required in root.get('children', []):
                _, child_created = PartCategory.objects.update_or_create(
                    normalized_name=normalize_category_name(child_name),
                    defaults={
                        'name': child_name,
                        'parent': category,
                        'aliases': aliases,
                        'fitment_required': fitment_required,
                    },
                )
                created_count += int(child_created)
        return created_count

    @classmethod
    def enable_tenant_catalog_domain(cls, tenant, domain_slug: str, seed_templates: bool = True) -> int:
        domain = CatalogDomain.objects.filter(slug=domain_slug, is_active=True).first()
        if domain is None:
            return 0
        TenantCatalogDomain.objects.update_or_create(
            tenant=tenant,
            domain=domain,
            defaults={'is_enabled': True},
        )
        if seed_templates:
            return cls.seed_tenant_primary_categories(tenant, domain)
        return 0

    @classmethod
    def seed_tenant_primary_categories(cls, tenant, root_domain: CatalogDomain) -> int:
        """Создаёт внутренний справочник и приоритетное дерево выбора категорий."""
        created_count = cls.seed_tenant_default_categories(tenant, root_domain)
        if root_domain.slug == TenantCatalogCategory.Domain.AUTO_PARTS:
            from apps.marketplaces.avito_tree_import import AvitoTreeImporter, has_tree

            if has_tree(root_domain.slug):
                created_count += AvitoTreeImporter(root_domain.slug).import_for_tenant(tenant)
        return created_count

    @classmethod
    def seed_tenant_default_categories(cls, tenant, root_domain: CatalogDomain | None = None) -> int:
        root_domain = root_domain or CatalogDomain.objects.filter(
            slug=TenantCatalogCategory.Domain.AUTO_PARTS,
        ).first()
        if root_domain is None:
            return 0
        category_tree = BASE_CATEGORY_TEMPLATE_TREE.get(root_domain.slug, [])
        if not category_tree:
            return 0
        seed_source = f'platform_{root_domain.slug}_seed'
        created_count = 0
        for root in category_tree:
            root_category, created = TenantCatalogCategory.objects.get_or_create(
                tenant=tenant,
                parent__isnull=True,
                normalized_name=normalize_category_name(root['name']),
                defaults={
                    'name': root['name'],
                    'root_domain': root_domain,
                    'domain': root_domain.slug,
                    'aliases': root.get('aliases', []),
                    'external_source': seed_source,
                    'external_id': f"root:{normalize_category_name(root['name'])}",
                    'is_active': True,
                },
            )
            created_count += int(created)
            if not created:
                cls._merge_aliases(root_category, root.get('aliases', []))
            for child in root.get('children', []):
                if isinstance(child, tuple):
                    child_name, aliases = child[0], child[1]
                else:
                    child_name, aliases = child, []
                child_category, child_created = TenantCatalogCategory.objects.get_or_create(
                    tenant=tenant,
                    parent=root_category,
                    normalized_name=normalize_category_name(child_name),
                    defaults={
                        'name': child_name,
                        'root_domain': root_domain,
                        'domain': root_domain.slug,
                        'aliases': aliases,
                        'external_source': seed_source,
                        'external_id': f"{normalize_category_name(root['name'])}:{normalize_category_name(child_name)}",
                        'is_active': True,
                    },
                )
                created_count += int(child_created)
                if not child_created:
                    cls._merge_aliases(child_category, aliases)
        return created_count


class ProductBrandService:
    """Platform-level normalization of product brands without replacing raw source text."""

    @staticmethod
    def normalize_brand(brand: str) -> str:
        return normalize_part_code(brand)

    @classmethod
    def resolve_or_create_brand(
        cls, brand_name: str, source_id: str = '', confidence: float = 0.8,
        needs_review: bool = False,
    ) -> ProductBrand | None:
        brand_name = (brand_name or '').strip()
        normalized = cls.normalize_brand(brand_name)
        if not normalized:
            return None

        alias = ProductBrandAlias.objects.select_related('brand').filter(
            normalized_alias=normalized,
            brand__is_active=True,
        ).first()
        if alias is not None:
            return alias.brand

        brand, created = ProductBrand.objects.get_or_create(
            normalized_name=normalized,
            defaults={
                'name': brand_name[:150],
                'source_id': source_id[:50],
                'confidence': confidence,
                'needs_review': needs_review,
                'is_active': True,
            },
        )
        if not created:
            update_fields = ['updated_at']
            if source_id and not brand.source_id:
                brand.source_id = source_id[:50]
                update_fields.append('source_id')
            if confidence > brand.confidence:
                brand.confidence = confidence
                update_fields.append('confidence')
            if needs_review and not brand.needs_review:
                brand.needs_review = True
                update_fields.append('needs_review')
            if update_fields != ['updated_at']:
                brand.save(update_fields=update_fields)

        ProductBrandAlias.objects.get_or_create(
            normalized_alias=normalized,
            defaults={
                'brand': brand,
                'alias': brand_name[:150],
                'source_id': source_id[:50],
                'confidence': confidence,
                'needs_review': needs_review,
            },
        )
        return brand


class QuotaExceeded(Exception):
    """Превышен лимит AI-генераций для тенанта."""


class AutoPartsEnrichmentDisabled(Exception):
    """Автозапчастное обогащение отключено для домена каталога tenant-а."""


class ProductIsNotAutoPart(Exception):
    """Товар не похож на автозапчасть для смешанного каталога tenant-а."""


class ProductService:
    """Сервис управления товарами: создание/обновление из источников данных."""

    @staticmethod
    def upsert_from_source(tenant, datasource, data: dict) -> tuple[Product, str, str | None]:
        """
        Создаёт или обновляет товар из данных адаптера.

        Возвращает (product, status, change_type) где:
        - status: 'created' | 'updated' | 'unchanged'
        - change_type: 'price_only' | 'stock_only' | 'content' | 'category' | None

        Unchanged означает что данные не изменились — задача в Celery не нужна.
        """
        hash_new = _compute_hash(data)
        uuid_1c = data.get('uuid') or None
        incoming_brand = str(data.get('brand') or '').strip()
        brand_source_id = getattr(datasource, 'type', '') or 'datasource'

        lookup = {'tenant': tenant, 'datasource': datasource, 'article': data['article']}
        defaults = {
            'name': data.get('name', ''),
            'brand': incoming_brand,
            'brand_ref': ProductBrandService.resolve_or_create_brand(
                incoming_brand,
                source_id=brand_source_id,
            ),
            'brand_resolution_status': (
                Product.BrandResolutionStatus.SOURCE
                if incoming_brand else Product.BrandResolutionStatus.UNKNOWN
            ),
            'brand_confidence': 1.0 if incoming_brand else 0.0,
            'brand_source_id': brand_source_id if incoming_brand else '',
            'brand_needs_review': False,
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

        if existing and existing.sync_excluded:
            return existing, 'unchanged', None

        # Источник не знает бренд, а у товара он есть (дозаполнен тенантом
        # вручную для Avito) — не затираем пустотой при каждом импорте.
        if existing and not defaults['brand'] and existing.brand:
            defaults['brand'] = existing.brand
            defaults['brand_ref'] = existing.brand_ref
            defaults['brand_resolution_status'] = existing.brand_resolution_status
            defaults['brand_confidence'] = existing.brand_confidence
            defaults['brand_source_id'] = existing.brand_source_id
            defaults['brand_needs_review'] = existing.brand_needs_review

        product, created = Product.objects.update_or_create(**lookup, defaults=defaults)
        if created:
            return product, 'created', None
        if old_hash != hash_new:
            old_data = {
                'price': str(existing.price),
                'stock_qty': existing.stock_qty,
                'name': existing.name,
                'brand': existing.brand,
                'condition': existing.condition,
                'category': existing.category_1c,
            }
            new_data = {
                'price': data.get('price', '0'),
                'stock_qty': data.get('stock_qty', 0),
                'name': data.get('name', ''),
                'brand': defaults['brand'],
                'condition': data.get('condition', 'new'),
                'category': data.get('category', ''),
            }
            change_type = ProductService.detect_change_type(old_data, new_data)
            return product, 'updated', change_type
        return product, 'unchanged', None

    @staticmethod
    def schedule_ai_generation(
        product, tenant, source_id: str = DEFAULT_PART_SOURCE,
    ) -> dict:
        """
        Проверяет лимит AI-кредитов и ставит генерацию описания в очередь.

        Для автозапчастей без trusted применяемости сначала запускает enrichment,
        чтобы агент получил данные из global graph или внешнего источника.

        Raises:
            QuotaExceeded: превышен лимит AI-генераций тенанта.
        """
        from apps.billing.services import LimitChecker
        from django.db import transaction

        can, reason = LimitChecker().can_generate_ai(tenant)
        if not can:
            raise QuotaExceeded(reason)
        if ProductEnrichmentService.should_enrich_before_ai(tenant, product):
            job = ProductEnrichmentService.create_parse_job(
                tenant=tenant,
                product=product,
                brand=product.brand,
                article=product.article,
                normalized_article=normalize_cross_code(product.article),
                source_id=source_id,
            )
            from apps.products.tasks import parse_single_part_then_generate_description
            transaction.on_commit(lambda: parse_single_part_then_generate_description.delay(job.pk))
            return {
                'mode': 'enrich_then_generate',
                'job_id': job.pk,
            }
        product_id = product.pk
        transaction.on_commit(lambda: _enqueue_ai_generation(product_id))
        return {
            'mode': 'generate',
            'job_id': None,
        }

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


# Маркеры неосновных веток дерева Avito для авто-классификации категорий.
# Ключ — normalized_name узла-ветки, значение — (префиксы слов, точные слова)
# текста товара, при которых товар действительно относится к этой ветке.
# Без таких признаков категории ветки получают штраф: имена узлов дублируются
# между ветками («Тормозная система» есть у легковых и грузовиков), и без
# штрафа легковые запчасти уходили в грузовую/мото ветки.
_TRUCK_MARKERS = (
    ('грузов', 'спецтехн'),
    {'камаз', 'краз', 'зил', 'автобус', 'трактор', 'экскаватор', 'погрузчик', 'тягач', 'полуприцеп'},
)
_MOTO_MARKERS = (
    ('мотоцикл', 'скутер', 'квадроцикл', 'мопед', 'снегоход', 'питбайк'),
    {'мото'},
)
_WATER_MARKERS = (
    ('лодочн', 'гидроцикл'),
    {'лодка', 'лодки', 'катер', 'яхта'},
)
_SECONDARY_BRANCH_MARKERS = {
    'длягрузовиковиспецтехники': _TRUCK_MARKERS,
    'шиныдлягрузовиковиспецтехники': _TRUCK_MARKERS,
    'длямототехники': _MOTO_MARKERS,
    'мотошины': _MOTO_MARKERS,
    'длямотоиводноготранспорта': (
        _MOTO_MARKERS[0] + _WATER_MARKERS[0],
        _MOTO_MARKERS[1] | _WATER_MARKERS[1],
    ),
    'дляводноготранспорта': _WATER_MARKERS,
    'прицепы': (('прицеп',), set()),
    'экипировка': (('экипир', 'мотошлем', 'мотоперчат'), {'шлем'}),
}


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
        cls, product: Product, save: bool = True, force: bool = False,
    ) -> ProductCatalogClassification:
        tenant_category = cls.get_product_tenant_category(product)
        text = ' '.join([
            product.name or '',
            product.category_1c or '',
            product.description_1c or '',
            product.brand or '',
        ]).lower()
        auto_matches = [marker for marker in cls.AUTO_PARTS_MARKERS if marker in text]
        non_auto_matches = [marker for marker in cls.NON_AUTO_PARTS_MARKERS if marker in text]

        if tenant_category and tenant_category.domain != TenantCatalogCategory.Domain.UNKNOWN:
            domain = tenant_category.domain
            confidence = 0.85
            reason = f'Тип товара определён по категории каталога: {tenant_category.name}.'
            needs_review = bool(
                domain == ProductCatalogClassification.Domain.AUTO_PARTS and non_auto_matches
            )
            if cls._is_auto_parts_fallback_category(tenant_category):
                # Категория присвоена общим фолбэком, а не подобрана по тексту —
                # тенант должен увидеть такой товар в очереди на проверку.
                needs_review = True
                reason = (
                    'Вид запчасти не определён по названию товара — присвоен общий узел '
                    '«Автомобиль на запчасти». Уточните категорию.'
                )
        elif auto_matches:
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
            'review_status': ReviewStatus.PENDING,
        }
        if save:
            classification, created = ProductCatalogClassification.objects.get_or_create(
                tenant=product.tenant,
                product=product,
                defaults=defaults,
            )
            if not created:
                if classification.source == ProductCatalogClassification.Source.MANUAL and not force:
                    return classification
                for field, value in defaults.items():
                    setattr(classification, field, value)
                classification.save(update_fields=[
                    'domain', 'confidence', 'source', 'reason',
                    'needs_review', 'review_status', 'updated_at',
                ])
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
        if classification.review_status == ReviewStatus.REJECTED:
            return False
        return (
            classification.domain == ProductCatalogClassification.Domain.AUTO_PARTS
            and classification.confidence >= 0.7
            and (
                classification.review_status == ReviewStatus.APPROVED
                or not classification.needs_review
            )
        )

    @staticmethod
    def product_catalog_supports_auto_parts(product: Product) -> bool:
        category = product.catalog_category
        if category is None or category.root_domain is None:
            return False
        return category.root_domain.supports_auto_parts_enrichment

    @staticmethod
    def get_product_tenant_category(product: Product) -> TenantCatalogCategory | None:
        if product.catalog_category_id:
            return product.catalog_category
        if not product.category_1c:
            return ProductEnrichmentService.infer_product_tenant_category(product)
        mapping = (
            TenantCategoryMapping.objects
            .select_related('category')
            .filter(
                tenant=product.tenant,
                source_category=product.category_1c,
                # Маппинг на отключённую категорию не применяем — тенант
                # выключил ветку, товары не должны в неё возвращаться.
                category__is_active=True,
            )
            .first()
        )
        if mapping is not None:
            product.catalog_category = mapping.category
            product.save(update_fields=['catalog_category', 'updated_at'])
            return mapping.category
        return ProductEnrichmentService.infer_product_tenant_category(product)

    @staticmethod
    def _category_word_matches(product_words: set[str], term_word: str) -> bool:
        if term_word in product_words:
            return True
        if len(term_word) < 4:
            return False

        for product_word in product_words:
            if len(product_word) < 4:
                continue
            stem_length = 3 if min(len(term_word), len(product_word)) <= 4 else 5
            if term_word[:stem_length] == product_word[:stem_length]:
                return True
            if len(product_word) == 4 and term_word.startswith(product_word):
                return True
            if len(term_word) == 4 and product_word.startswith(term_word):
                return True
        return False

    # Слова без классифицирующего смысла: встречаются почти в каждом названии
    # товара/категории 1С продавца запчастей и давали ложные полные совпадения
    # узлам «Автомобиль на запчасти», «Для автомобилей», «Запчасти».
    CATEGORY_TERM_STOP_WORDS = {
        'для',
        'авто', 'автомобиль', 'автомобиля', 'автомобилей', 'автомобили',
        'запчасти', 'запчасть', 'автозапчасти',
        'другое', 'другие', 'прочее', 'прочие',
    }

    @staticmethod
    def _category_match_score(product_words: set[str], normalized_text: str, category: TenantCatalogCategory) -> float:
        score = 0.0
        stop_words = ProductEnrichmentService.CATEGORY_TERM_STOP_WORDS
        terms = [category.name, *category.aliases]
        for index, term in enumerate(terms):
            words = re.findall(r'[0-9a-zа-яё]+', term.lower())
            words = [word for word in words if len(word) > 2 and word not in stop_words]
            if not words:
                continue

            matched_count = sum(
                1 for word in words
                if ProductEnrichmentService._category_word_matches(product_words, word)
            )
            if matched_count == 0:
                continue

            term_score = float(matched_count)
            if matched_count == len(words):
                term_score += len(words)
            if normalize_category_name(term) in normalized_text:
                term_score += len(words) * 2
            if index == 0:
                term_score += 0.25
            score = max(score, term_score)

        if score > 0 and category.parent_id:
            score += 0.1
        return score

    @classmethod
    def infer_product_tenant_category(cls, product: Product) -> TenantCatalogCategory | None:
        text = dehomoglyph(' '.join([
            product.name or '',
            product.category_1c or '',
            product.description_1c or '',
            product.brand or '',
        ])).lower()
        product_words = set(re.findall(r'[0-9a-zа-яё]+', text))
        normalized_text = normalize_category_name(text)
        if not product_words or not normalized_text:
            return None

        enabled_domain_ids = TenantCatalogDomain.objects.filter(
            tenant=product.tenant,
            is_enabled=True,
        ).values_list('domain_id', flat=True)
        categories = list(
            TenantCatalogCategory.objects
            .filter(
                tenant=product.tenant,
                is_active=True,
                root_domain_id__in=enabled_domain_ids,
            )
            .select_related('parent', 'root_domain')
        )
        # Полное дерево Avito оптимально для ручного выбора и публикации, но его
        # широкие узлы («Автосвет», «Тормозная система») уступают компактному
        # внутреннему справочнику в точности автоопределения. Когда справочник
        # доступен, Avito-категории не участвуют в автоматическом матчинге.
        has_internal_auto_parts_tree = any(
            category.root_domain_id
            and category.root_domain.slug == TenantCatalogCategory.Domain.AUTO_PARTS
            and category.external_source == ProductCategorySeedService.SEED_SOURCE
            and category.external_id
            for category in categories
        )
        if has_internal_auto_parts_tree:
            categories = [
                category
                for category in categories
                if not (
                    category.root_domain_id
                    and category.root_domain.slug == TenantCatalogCategory.Domain.AUTO_PARTS
                    and category.external_source == 'avito'
                )
            ]
        categories_by_id = {category.id: category for category in categories}

        best_category = None
        best_key = None
        best_score = 0.0
        for category in categories:
            score = cls._category_match_score(product_words, normalized_text, category)
            if score <= 0:
                continue
            branch_key = cls._secondary_branch_key(category, categories_by_id)
            if branch_key is not None:
                if cls._product_matches_branch(product_words, branch_key):
                    # Признаки ветки в тексте товара — при равном счёте ветка
                    # должна победить одноимённый узел легковой ветки.
                    score += 0.05
                else:
                    score *= 0.3
            # Tie-break детерминированный: счёт → глубина (специфичнее лучше)
            # → меньший id; раньше победитель ничьей зависел от порядка выборки.
            key = (score, cls._category_depth(category, categories_by_id), -category.id)
            if best_key is None or key > best_key:
                best_category = category
                best_key = key
                best_score = score

        if best_category is None or best_score < 2:
            # Фолбэк: не определили вид, но домен авто — ставим общий публикуемый
            # узел Avito «Автомобиль на запчасти» (валидный SparePartType), чтобы
            # товар всё равно публиковался без ошибки. Тенант уточнит позже.
            best_category = cls._auto_parts_fallback_category(product, categories)
            if best_category is None:
                return None

        product.catalog_category = best_category
        product.save(update_fields=['catalog_category', 'updated_at'])
        return best_category

    @staticmethod
    def _secondary_branch_key(category, categories_by_id: dict) -> str | None:
        """Нормализованное имя неосновной ветки Avito (грузовики/мото/…), в которой лежит категория."""
        node = category
        seen: set[int] = set()
        while node is not None and node.id not in seen:
            seen.add(node.id)
            if node.normalized_name in _SECONDARY_BRANCH_MARKERS:
                return node.normalized_name
            node = categories_by_id.get(node.parent_id) if node.parent_id else None
        return None

    @staticmethod
    def _product_matches_branch(product_words: set[str], branch_key: str) -> bool:
        """Есть ли в тексте товара признаки принадлежности к неосновной ветке Avito."""
        prefixes, exact_words = _SECONDARY_BRANCH_MARKERS[branch_key]
        if exact_words & product_words:
            return True
        return any(word.startswith(prefix) for word in product_words for prefix in prefixes)

    @staticmethod
    def _category_depth(category, categories_by_id: dict) -> int:
        """Глубина категории в дереве каталога (по загруженным категориям)."""
        depth = 0
        node = category
        seen: set[int] = set()
        while node is not None and node.parent_id and node.id not in seen:
            seen.add(node.id)
            depth += 1
            node = categories_by_id.get(node.parent_id)
        return depth

    @staticmethod
    def _auto_parts_fallback_category(product, categories) -> 'TenantCatalogCategory | None':
        """Общий публикуемый узел Avito для авто-домена, если вид не определился."""
        from apps.products.part_category_seed import normalize_category_name
        target = normalize_category_name('Автомобиль на запчасти')
        for category in categories:
            if category.normalized_name == target and category.external_source == 'avito':
                return category
        return None

    @staticmethod
    def _is_auto_parts_fallback_category(category) -> bool:
        """Является ли категория общим фолбэк-узлом «Автомобиль на запчасти»."""
        if category is None:
            return False
        from apps.products.part_category_seed import normalize_category_name
        return (
            category.external_source == 'avito'
            and category.normalized_name == normalize_category_name('Автомобиль на запчасти')
        )

    @classmethod
    def ensure_product_auto_parts_eligible(cls, tenant, product: Product | None) -> None:
        if product is None and getattr(tenant, 'requires_product_auto_parts_check', False):
            raise ProductIsNotAutoPart(
                'Для смешанного каталога нужно указать товар, чтобы проверить, что это автозапчасть.'
            )
        if product is None:
            cls.ensure_auto_parts_enabled(tenant)
            return

        product_category_supports = cls.product_catalog_supports_auto_parts(product)
        if not getattr(tenant, 'supports_auto_parts_enrichment', True) and not product_category_supports:
            raise AutoPartsEnrichmentDisabled(
                'Автозапчастное обогащение доступно только для каталога автозапчастей.'
            )
        if (
            (
                getattr(tenant, 'requires_product_auto_parts_check', False)
                or product_category_supports
            )
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
    def apply_approved_fact(cls, product: Product, fact: ProductEnrichmentFact) -> None:
        """Promote a reviewed web-research brand/OEM claim into trusted product data."""
        if fact.review_status != ReviewStatus.APPROVED:
            return
        if fact.fact_type == ProductEnrichmentFact.FactType.BRAND:
            brand = str(fact.value or '').strip()[:200]
            if not brand:
                return
            product.brand = brand
            product.brand_ref = ProductBrandService.resolve_or_create_brand(
                brand,
                source_id='human_review',
                confidence=1.0,
            )
            product.brand_resolution_status = Product.BrandResolutionStatus.MANUAL
            product.brand_confidence = 1.0
            product.brand_source_id = 'human_review'
            product.brand_needs_review = False
            product.save(update_fields=[
                'brand', 'brand_ref', 'brand_resolution_status', 'brand_confidence',
                'brand_source_id', 'brand_needs_review', 'updated_at',
            ])
            return

        if fact.fact_type != ProductEnrichmentFact.FactType.OEM:
            return
        payload = {}
        try:
            raw = json.loads(fact.raw_text or '{}')
            payload = raw.get('claim_payload') or {}
        except (TypeError, ValueError):
            pass
        code = str(payload.get('code') or fact.value or '').strip()[:100]
        normalized = normalize_cross_code(code)
        if not normalized:
            return
        code_type = str(payload.get('code_type') or ProductCrossCode.CodeType.OEM)
        if code_type not in ProductCrossCode.CodeType.values:
            code_type = ProductCrossCode.CodeType.UNKNOWN
        cross, _ = ProductCrossCode.objects.get_or_create(
            tenant=product.tenant,
            product=product,
            source_id='human_review',
            manufacturer=str(payload.get('manufacturer') or fact.name or '')[:100],
            normalized_code=normalized,
            code_type=code_type,
            defaults={'code': code},
        )
        cls.refresh_product_denormalized_enrichment(product)
        product.save(update_fields=['oem_numbers', 'cross_numbers', 'applicability', 'updated_at'])
        ProductKnowledgeGraphService.learn_approved_cross_code(product, cross)

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
    def has_trusted_fitments(cls, product: Product) -> bool:
        return any(
            should_auto_apply_fitment(fitment)
            for fitment in product.fitments.all()
        )

    @classmethod
    def should_enrich_before_ai(cls, tenant, product: Product) -> bool:
        if not getattr(tenant, 'supports_auto_parts_enrichment', True):
            return False
        if not product.article:
            return False
        if (
            getattr(tenant, 'requires_product_auto_parts_check', False)
            and not cls.is_product_auto_part_candidate(product)
        ):
            return False
        return not cls.has_trusted_fitments(product)

    @classmethod
    def save_parsed_part(
        cls, tenant, product: Product, parsed: ParsedPart, source_id: str = DEFAULT_PART_SOURCE,
    ) -> None:
        """Сохраняет enrichment-данные, не трогая цену, остаток и склад."""
        from django.db import transaction

        cls._ensure_product_tenant(product, tenant)
        with transaction.atomic():
            brand_conflict = bool(
                parsed.brand
                and product.brand
                and ProductBrandService.normalize_brand(parsed.brand)
                != ProductBrandService.normalize_brand(product.brand)
            )
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
                cls._build_vehicle_fitment(
                    tenant,
                    product,
                    fitment,
                    source_id,
                    parsed.source_url,
                    force_review=brand_conflict,
                )
                for fitment in parsed.fitments
                if fitment.model
            ], ignore_conflicts=True)

            description_facts = [
                cls._build_enrichment_fact(
                    tenant, product, source_id, parsed.source_url,
                    ProductEnrichmentFact.FactType.DESCRIPTION_HINT,
                    name, value,
                    force_review=brand_conflict,
                )
                for name, value in parsed.description_facts.items()
                if name and value
            ]
            ProductEnrichmentFact.objects.bulk_create(description_facts, ignore_conflicts=True)

            cls.refresh_product_denormalized_enrichment(product)
            product_update_fields = ['oem_numbers', 'cross_numbers', 'applicability']
            cls._apply_catalog_brand(
                product,
                parsed.brand,
                source_id,
                product_update_fields,
            )
            product.save(update_fields=product_update_fields)
            if not brand_conflict:
                ProductKnowledgeGraphService.learn_from_parsed_part(
                    product=product,
                    parsed=parsed,
                    source_id=source_id,
                )

    @staticmethod
    def _apply_catalog_brand(
        product: Product,
        parsed_brand: str,
        source_id: str,
        update_fields: list[str],
    ) -> None:
        """Backfill an absent brand or flag a non-manual conflicting catalogue result."""
        parsed_brand = (parsed_brand or '').strip()[:200]
        if not parsed_brand:
            return
        try:
            confidence = float(get_part_source_config(source_id).get('trust_score', 0.8))
        except ValueError:
            confidence = 0.8

        current_normalized = ProductBrandService.normalize_brand(product.brand)
        parsed_normalized = ProductBrandService.normalize_brand(parsed_brand)
        if not current_normalized:
            product.brand = parsed_brand
            product.brand_ref = ProductBrandService.resolve_or_create_brand(
                parsed_brand,
                source_id=source_id,
                confidence=confidence,
            )
            product.brand_resolution_status = Product.BrandResolutionStatus.CATALOG
            product.brand_confidence = confidence
            product.brand_source_id = source_id[:50]
            product.brand_needs_review = False
            update_fields.extend([
                'brand', 'brand_ref', 'brand_resolution_status', 'brand_confidence',
                'brand_source_id', 'brand_needs_review',
            ])
            return

        if current_normalized == parsed_normalized:
            if product.brand_ref_id is None:
                product.brand_ref = ProductBrandService.resolve_or_create_brand(
                    parsed_brand,
                    source_id=source_id,
                    confidence=confidence,
                )
                update_fields.append('brand_ref')
            return

        if product.brand_resolution_status == Product.BrandResolutionStatus.MANUAL:
            return
        product.brand_resolution_status = Product.BrandResolutionStatus.AMBIGUOUS
        product.brand_confidence = min(product.brand_confidence or confidence, confidence, 0.5)
        product.brand_source_id = '|'.join(filter(None, [
            product.brand_source_id,
            source_id,
        ]))[:50]
        product.brand_needs_review = True
        update_fields.extend([
            'brand_resolution_status', 'brand_confidence',
            'brand_source_id', 'brand_needs_review',
        ])

    @classmethod
    def _build_vehicle_fitment(
        cls, tenant, product: Product, parsed_fitment, source_id: str, source_url: str,
        *, force_review: bool = False,
    ) -> VehicleFitment:
        fitment = VehicleFitment(
            tenant=tenant,
            product=product,
            source_id=source_id,
            source_url=source_url,
            make=parsed_fitment.make[:100],
            model=parsed_fitment.model[:150],
            generation=parsed_fitment.generation[:100],
            date_from=parsed_fitment.date_from[:20],
            date_to=parsed_fitment.date_to[:20],
            modification=parsed_fitment.modification[:255],
            engine_code=parsed_fitment.engine_code[:100],
            power_hp=parsed_fitment.power_hp,
            raw_text=parsed_fitment.raw_text,
            confidence=parsed_fitment.confidence,
            needs_review=parsed_fitment.needs_review,
            last_seen_at=now(),
        )
        existing = product.fitments.all()
        if force_review or should_mark_needs_review(
            fitment,
            has_conflict=has_conflicting_fitment(existing, fitment),
        ):
            fitment.needs_review = True
        return fitment

    @classmethod
    def _build_enrichment_fact(
        cls, tenant, product: Product, source_id: str, source_url: str,
        fact_type: str, name: str, value: str,
        *, force_review: bool = False,
    ) -> ProductEnrichmentFact:
        fact = ProductEnrichmentFact(
            tenant=tenant,
            product=product,
            source_id=source_id,
            source_url=source_url,
            fact_type=fact_type,
            name=name[:150],
            value=value,
            value_hash=make_value_hash(value),
            confidence=1.0,
            needs_review=False,
            last_seen_at=now(),
        )
        existing = product.enrichment_facts.filter(fact_type=fact_type, name=name[:150])
        if force_review or should_mark_needs_review(
            fact,
            has_conflict=has_conflicting_fact(existing, fact),
        ):
            fact.needs_review = True
        return fact

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
            if not should_auto_apply_fitment(fitment):
                continue
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
            if hasattr(parser, 'set_tenant'):
                parser.set_tenant(job.tenant)
            try:
                html, source_url = parser.fetch(job.brand, job.article)
                parsed = parser.parse_html(html, job.brand, job.article, source_url=source_url)
            except PartNotFound:
                if not hasattr(parser, 'fetch_search'):
                    raise
                # Название товара помогает выбрать нужный товар, когда один
                # артикул принадлежит разным брендам/деталям.
                hint = ' '.join(filter(None, [getattr(product, 'name', ''), job.brand])).strip()
                html, source_url = parser.fetch_search(job.article, hint=hint)
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
        ProductBulkActionJob.Action.CLASSIFY_CATALOG_DOMAIN,
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
                elif job.action == ProductBulkActionJob.Action.GENERATE_DESCRIPTIONS:
                    try:
                        ProductService.schedule_ai_generation(product, job.tenant, source_id=job.source_id)
                    except QuotaExceeded:
                        job.skipped_count += 1
                        continue
                    queued += 1
                elif job.action == ProductBulkActionJob.Action.CLASSIFY_CATALOG_DOMAIN:
                    ProductEnrichmentService.classify_product_catalog_domain(product)
                    queued += 1
                elif job.action == ProductBulkActionJob.Action.FIND_IMAGES:
                    from apps.image_search.tasks import search_images_for_product
                    transaction.on_commit(
                        lambda pk=product.pk: search_images_for_product.delay(pk),
                    )
                    queued += 1
                else:
                    job.skipped_count += 1

            job.processed_count += len(batch_ids)
            job.queued_count += queued
            if job.action == ProductBulkActionJob.Action.CLASSIFY_CATALOG_DOMAIN:
                job.success_count += queued
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
                'success_count', 'skipped_count', 'finished_at', 'next_batch_at', 'updated_at',
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
    def normalize_generation(cls, value: str) -> str:
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
    def upsert_generation(
        cls, model: VehicleModel, name: str, date_from: str = '', date_to: str = '',
    ) -> VehicleGeneration | None:
        normalized = cls.normalize_generation(name)
        if not normalized:
            return None
        generation, created = VehicleGeneration.objects.get_or_create(
            model=model,
            normalized_name=normalized,
            defaults={
                'name': name.strip()[:100],
                'body_code': name.strip()[:50],
                'date_from': date_from[:20],
                'date_to': date_to[:20],
                'aliases': [name.strip()] if name.strip() else [],
            },
        )
        if not created:
            update_fields = ['updated_at']
            aliases_before = list(generation.aliases)
            cls._append_alias(generation, name, save=False)
            if aliases_before != generation.aliases:
                update_fields.append('aliases')
            if date_from and not generation.date_from:
                generation.date_from = date_from[:20]
                update_fields.append('date_from')
            if date_to and not generation.date_to:
                generation.date_to = date_to[:20]
                update_fields.append('date_to')
            if update_fields != ['updated_at']:
                generation.save(update_fields=update_fields)
        return generation

    @classmethod
    def resolve_fitment(
        cls, fitment,
    ) -> tuple[VehicleMake | None, VehicleModel | None, VehicleGeneration | None]:
        make = cls.upsert_make(fitment.make)
        if make is None:
            return None, None, None
        model = cls.upsert_model(make, fitment.model)
        if model is None:
            return make, None, None
        generation = cls.upsert_generation(
            model,
            fitment.generation,
            date_from=fitment.date_from,
            date_to=fitment.date_to,
        )
        return make, model, generation

    @staticmethod
    def _append_alias(instance, alias: str, save: bool = True) -> None:
        alias = (alias or '').strip()
        if not alias or alias in instance.aliases:
            return
        instance.aliases = [*instance.aliases, alias][:50]
        if save:
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
    def has_trusted_product_identity(cls, product: Product) -> bool:
        return bool(
            cls.normalize_brand(product.brand)
            and not product.brand_needs_review
            and product.brand_resolution_status != Product.BrandResolutionStatus.AMBIGUOUS
        )

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
                'brand_ref': ProductBrandService.resolve_or_create_brand(
                    brand,
                    source_id=source_id or DEFAULT_PART_SOURCE,
                    confidence=confidence,
                    needs_review=needs_review,
                ),
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
            if part.brand_ref_id is None:
                part.brand_ref = ProductBrandService.resolve_or_create_brand(
                    brand,
                    source_id=source_id or DEFAULT_PART_SOURCE,
                    confidence=confidence,
                    needs_review=needs_review,
                )
                if part.brand_ref_id is not None:
                    update_fields.append('brand_ref')
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
            if confidence > part.confidence and can_raise_confidence(part.source_id, source_id):
                part.confidence = confidence
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
        elif not should_auto_apply_relation(relation):
            relation.needs_review = True
            relation.save(update_fields=['needs_review', 'updated_at'])
        return relation

    @classmethod
    def upsert_fitment(
        cls, part: GlobalPart, fitment, source_id: str = '',
        source_url: str = '',
    ) -> GlobalPartFitment:
        vehicle_make, vehicle_model, vehicle_generation = VehicleKnowledgeService.resolve_fitment(fitment)
        incoming_needs_review = fitment.needs_review
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
                'vehicle_generation': vehicle_generation,
                'date_from': fitment.date_from[:20],
                'date_to': fitment.date_to[:20],
                'source_url': source_url,
                'raw_text': fitment.raw_text,
                'confidence': fitment.confidence,
                'needs_review': incoming_needs_review,
                'last_seen_at': now(),
            },
        )
        if not created:
            global_fitment.last_seen_at = now()
            global_fitment.confidence = max(global_fitment.confidence, fitment.confidence)
            global_fitment.needs_review = global_fitment.needs_review or incoming_needs_review
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
            if vehicle_generation and global_fitment.vehicle_generation_id is None:
                global_fitment.vehicle_generation = vehicle_generation
            global_fitment.save(update_fields=[
                'last_seen_at', 'confidence', 'needs_review', 'date_from',
                'date_to', 'source_url', 'raw_text', 'vehicle_make',
                'vehicle_model', 'vehicle_generation', 'updated_at',
            ])
        elif (
            has_conflicting_fitment(part.fitments.exclude(pk=global_fitment.pk), global_fitment)
            or not should_auto_apply_fitment(global_fitment)
        ):
            global_fitment.needs_review = True
            global_fitment.save(update_fields=['needs_review', 'updated_at'])
        return global_fitment

    @classmethod
    def learn_from_parsed_part(
        cls, product: Product, parsed: ParsedPart, source_id: str = DEFAULT_PART_SOURCE,
    ) -> None:
        if not cls.has_trusted_product_identity(product):
            return
        source_part = cls.upsert_part(
            brand=product.brand,
            article=product.article or parsed.article,
            title=parsed.title,
            source_id=source_id,
            source_url=parsed.source_url,
        )
        for cross in parsed.cross_codes:
            normalized = normalize_part_code(cross.code)
            if not normalized or not cls.normalize_brand(cross.manufacturer):
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
            if (
                not normalize_part_code(related.article)
                or not cls.normalize_brand(related.brand)
            ):
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
    def learn_approved_fitment(cls, product: Product, fitment: VehicleFitment) -> None:
        """Promote a human-approved product fitment into reusable platform knowledge."""
        if (
            not cls.has_trusted_product_identity(product)
            or not normalize_part_code(product.article)
            or not fitment.model
        ):
            return
        part = cls.upsert_part(
            brand=product.brand,
            article=product.article,
            title=product.name,
            source_id='human_review',
            source_url=fitment.source_url,
            confidence=1.0,
            needs_review=False,
        )
        approved = cls.upsert_fitment(
            part=part,
            fitment=fitment,
            source_id='human_review',
            source_url=fitment.source_url,
        )
        if approved.needs_review or approved.confidence < 1.0:
            approved.needs_review = False
            approved.confidence = 1.0
            approved.save(update_fields=['needs_review', 'confidence', 'updated_at'])

    @classmethod
    def learn_approved_cross_code(cls, product: Product, cross: ProductCrossCode) -> None:
        """Promote a human-approved OEM/Cross code into reusable platform knowledge."""
        if (
            not cls.has_trusted_product_identity(product)
            or not normalize_part_code(product.article)
            or not cls.normalize_brand(cross.manufacturer)
            or not normalize_part_code(cross.code)
        ):
            return
        source_part = cls.upsert_part(
            brand=product.brand,
            article=product.article,
            title=product.name,
            source_id='human_review',
            source_url='',
            confidence=1.0,
            needs_review=False,
        )
        target_part = cls.upsert_part(
            brand=cross.manufacturer,
            article=cross.code,
            source_id='human_review',
            source_url='',
            confidence=1.0,
            needs_review=False,
        )
        relation_type = cls.CROSS_TO_RELATION.get(
            cross.code_type,
            GlobalPartRelation.RelationType.UNKNOWN,
        )
        relation = cls.upsert_relation(
            source_part=source_part,
            target_part=target_part,
            relation_type=relation_type,
            source_id='human_review',
            source_url='',
            raw_text=f'{cross.manufacturer}: {cross.code}'.strip(': '),
            confidence=1.0,
            needs_review=False,
        )
        if relation.needs_review or relation.confidence < 1.0:
            relation.needs_review = False
            relation.confidence = 1.0
            relation.save(update_fields=['needs_review', 'confidence', 'updated_at'])

    @classmethod
    def apply_known_relations_to_product(cls, product: Product) -> int:
        if not cls.has_trusted_product_identity(product):
            return 0
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
        if not cls.has_trusted_product_identity(product):
            return 0
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
                    'source_url': fitment.source_url,
                    'raw_text': fitment.raw_text,
                    'confidence': fitment.confidence,
                    'needs_review': False,
                    'last_seen_at': now(),
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
