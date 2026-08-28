import hashlib
import io
import tempfile
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterable, cast
from xml.etree import ElementTree as ET

from django.conf import settings


# Соответствие значений condition из Product → Avito
_CONDITION_MAP = {
    'new': 'Новое',
    'used': 'Б/у',
    'refurbished': 'Б/у',
}

_DEFAULT_CATEGORY = 'Запчасти и аксессуары'
AVITO_TITLE_MAX_LENGTH = 200
AVITO_DESCRIPTION_MAX_LENGTH = 7500

# Legacy build_feed по публичному контракту возвращает bytes, поэтому размер
# готового документа там всё равно один раз оказывается в памяти вызывающего
# кода. Private artifact callers используют write_feed с отдельным disk-backed
# sink и не делают финальный read(); в обоих путях полное ElementTree для 10 000
# объявлений не создаётся.
_FEED_SPOOL_MEMORY_BYTES = 1024 * 1024
_FEED_BUILD_BATCH_SIZE = 500
_MISSING = object()


class FeedPayloadSizeExceeded(ValueError):
    """The generated XML crossed the caller's explicit byte ceiling."""


@dataclass(frozen=True, slots=True)
class FeedWriteResult:
    """Exact immutable metadata for bytes written to a caller-owned sink."""

    listing_count: int
    size_bytes: int
    payload_sha256: str


class _HashingBoundedWriter:
    """Hash complete writes while refusing an oversized or partial sink."""

    def __init__(self, sink: BinaryIO, *, max_bytes: int | None):
        write = getattr(sink, 'write', None)
        if not callable(write):
            raise TypeError('Feed sink must expose a binary write() method.')
        if max_bytes is not None and (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise ValueError('Feed byte ceiling must be a positive integer.')
        self._sink = sink
        self._max_bytes = max_bytes
        self._digest = hashlib.sha256()
        self.size_bytes = 0

    def write(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError('Feed XML writer accepts bytes only.')
        next_size = self.size_bytes + len(chunk)
        if self._max_bytes is not None and next_size > self._max_bytes:
            raise FeedPayloadSizeExceeded(
                f'Feed XML exceeds the {self._max_bytes}-byte ceiling.',
            )
        written = self._sink.write(chunk)
        if written != len(chunk):
            raise OSError('Feed sink did not accept the complete byte chunk.')
        self._digest.update(chunk)
        self.size_bytes = next_size

    @property
    def payload_sha256(self) -> str:
        return self._digest.hexdigest()


def _cached_relation(instance, relation_name: str):
    """Return a select_related value without accidentally issuing a query."""
    state = getattr(instance, '_state', None)
    fields_cache = getattr(state, 'fields_cache', None)
    if fields_cache is None:
        return _MISSING
    return fields_cache.get(relation_name, _MISSING)


def _prefetched_relation(instance, relation_name: str):
    cache = getattr(instance, '_prefetched_objects_cache', None)
    if cache is None:
        return _MISSING
    return cache.get(relation_name, _MISSING)


def _batched(iterable, size: int):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


class _FeedBuildContext:
    """Bounded, build-local caches for relations used by the Avito XML.

    The coordinator already selects listing/product/account. The builder owns
    the remaining fan-out relations and loads them per bounded batch, avoiding
    queries per listing while not retaining all image model instances for a
    10k feed at once.
    """

    def __init__(self):
        self._category_mappings = {}
        self._mapping_tenants_loaded = set()
        self._categories = {}
        self._category_specs = {}
        self._addresses = {}
        self._default_addresses = {}
        self._images_by_product = {}
        self._brand_lookups = {}

    def prepare_batch(self, listings) -> None:
        # These relations are batch-local. Keeping prior model instances would
        # turn a 10k generation back into an O(N) ORM-object cache even though
        # XML serialization itself is streaming.
        self._categories = {}
        self._category_specs = {}
        self._addresses = {}
        self._default_addresses = {}
        self._load_category_mappings(listings)
        self._load_categories(listings)
        self._load_addresses(listings)
        self._load_images(listings)

    def _load_category_mappings(self, listings) -> None:
        from apps.marketplaces.models import CategoryMapping

        tenant_ids = set()
        for listing in listings:
            tenant_id = getattr(listing, 'tenant_id', None)
            if tenant_id is None or tenant_id in self._mapping_tenants_loaded:
                continue
            tenant_ids.add(tenant_id)
            tenant = _cached_relation(listing, 'tenant')
            if tenant is _MISSING:
                continue
            prefetched = _prefetched_relation(tenant, 'category_mappings')
            if prefetched is _MISSING:
                continue
            for mapping in prefetched:
                if mapping.marketplace == CategoryMapping.MARKETPLACE_AVITO:
                    self._category_mappings[
                        (mapping.tenant_id, mapping.category_source)
                    ] = mapping
            self._mapping_tenants_loaded.add(tenant_id)

        missing = tenant_ids - self._mapping_tenants_loaded
        if not missing:
            return
        mappings = CategoryMapping.objects.filter(
            tenant_id__in=missing,
            marketplace=CategoryMapping.MARKETPLACE_AVITO,
        ).order_by('pk')
        for mapping in mappings:
            self._category_mappings[
                (mapping.tenant_id, mapping.category_source)
            ] = mapping
        self._mapping_tenants_loaded.update(missing)

    def category_mapping(self, listing):
        tenant_id = getattr(listing, 'tenant_id', None)
        if tenant_id not in self._mapping_tenants_loaded:
            return _get_category_mapping(listing)
        product = listing.product
        candidates = []
        if product.category_1c:
            candidates.append(product.category_1c)
        category = self.category_for(listing)
        if category is not None:
            candidates.append(category.name)
        for source in candidates:
            mapping = self._category_mappings.get((tenant_id, source))
            if mapping is not None:
                return mapping
        return None

    def _register_category(self, category) -> int | None:
        """Cache a loaded ancestry prefix and return its first missing parent."""

        if category is None:
            return None
        category_id = getattr(category, 'pk', None)
        if category_id is not None:
            self._categories.setdefault(category_id, category)
        parent = _cached_relation(category, 'parent')
        if parent is not _MISSING:
            return self._register_category(parent)
        parent_id = getattr(category, 'parent_id', None)
        if parent_id is not None and parent_id not in self._categories:
            return parent_id
        return None

    def _load_categories(self, listings) -> None:
        from apps.products.models import TenantCatalogCategory

        wanted_ids = set()
        for listing in listings:
            product = listing.product
            cached = _cached_relation(product, 'catalog_category')
            if cached is not _MISSING:
                missing_parent_id = self._register_category(cached)
                if missing_parent_id is not None:
                    wanted_ids.add(missing_parent_id)
                continue
            category_id = getattr(product, 'catalog_category_id', None)
            if category_id is not None:
                wanted_ids.add(category_id)

        pending = wanted_ids - self._categories.keys()
        while pending:
            categories = list(TenantCatalogCategory.objects.filter(pk__in=pending))
            if not categories:
                break
            for category in categories:
                self._register_category(category)
            pending = {
                category.parent_id for category in categories
                if category.parent_id is not None
                and category.parent_id not in self._categories
            }

    def category_for(self, listing):
        product = listing.product
        cached = _cached_relation(product, 'catalog_category')
        if cached is not _MISSING:
            self._register_category(cached)
            return cached
        category_id = getattr(product, 'catalog_category_id', None)
        if category_id is not None:
            return self._categories.get(category_id)
        # Supports lightweight, non-model objects used by adapter callers.
        return getattr(product, 'catalog_category', None)

    def parent_of(self, category):
        cached = _cached_relation(category, 'parent')
        if cached is not _MISSING:
            self._register_category(cached)
            return cached
        parent_id = getattr(category, 'parent_id', None)
        if parent_id is not None:
            return self._categories.get(parent_id)
        return getattr(category, 'parent', None)

    def avito_spec(self, listing) -> dict:
        category = self.category_for(listing)
        if category is None:
            return {}
        key = getattr(category, 'pk', None) or id(category)
        if key not in self._category_specs:
            self._category_specs[key] = _resolve_avito_spec(category, self.parent_of)
        return self._category_specs[key]

    def brand_for(self, listing, *, spec) -> str:
        return _get_brand(
            listing,
            spec=spec,
            lookup_cache=self._brand_lookups,
        )

    @staticmethod
    def _register_explicit_address(address, addresses) -> None:
        if address is not None and getattr(address, 'pk', None) is not None:
            addresses[address.pk] = address

    def _load_addresses(self, listings) -> None:
        from django.db.models import Q

        from apps.marketplaces.models import MarketplacePlacementAddress

        explicit_ids = set()
        account_ids_to_load = set()
        for listing in listings:
            for relation_name in ('placement_address', 'bulk_placement_address'):
                cached = _cached_relation(listing, relation_name)
                if cached is not _MISSING:
                    self._register_explicit_address(cached, self._addresses)
                    continue
                address_id = getattr(listing, f'{relation_name}_id', None)
                if address_id is not None and address_id not in self._addresses:
                    explicit_ids.add(address_id)

            account_id = getattr(listing, 'account_id', None)
            if account_id is None or account_id in self._default_addresses:
                continue
            account = _cached_relation(listing, 'account')
            if account is _MISSING:
                account = None
            prefetched = (
                _prefetched_relation(account, 'placement_addresses')
                if account is not None else _MISSING
            )
            if prefetched is not _MISSING:
                prefetched_addresses = cast(Iterable[Any], prefetched)
                defaults = sorted(
                    (
                        address for address in prefetched_addresses
                        if address.is_active and address.is_default
                    ),
                    key=lambda address: (address.name, address.pk),
                )
                self._default_addresses[account_id] = defaults[0] if defaults else None
                for address in prefetched_addresses:
                    self._register_explicit_address(address, self._addresses)
            else:
                account_ids_to_load.add(account_id)

        query = None
        if explicit_ids:
            query = Q(pk__in=explicit_ids)
        if account_ids_to_load:
            default_query = Q(
                account_id__in=account_ids_to_load,
                is_active=True,
                is_default=True,
            )
            query = default_query if query is None else query | default_query
        if query is not None:
            addresses = MarketplacePlacementAddress.objects.filter(query).order_by(
                'account_id', 'name', 'pk',
            )
            for address in addresses:
                self._addresses[address.pk] = address
                if (
                    address.account_id in account_ids_to_load
                    and address.is_active
                    and address.is_default
                    and address.account_id not in self._default_addresses
                ):
                    self._default_addresses[address.account_id] = address
        for account_id in account_ids_to_load:
            self._default_addresses.setdefault(account_id, None)

    def placement_sources(self, listing):
        def relation_value(relation_name):
            cached = _cached_relation(listing, relation_name)
            if cached is not _MISSING:
                return cached
            relation_id = getattr(listing, f'{relation_name}_id', None)
            if relation_id is not None:
                return self._addresses.get(relation_id)
            return getattr(listing, relation_name, None)

        return (
            relation_value('placement_address'),
            relation_value('bulk_placement_address'),
            self._default_addresses.get(getattr(listing, 'account_id', None)),
        )

    def _load_images(self, listings) -> None:
        from django.db.models import F, Prefetch, Window, prefetch_related_objects
        from django.db.models.functions import RowNumber

        from apps.media_processing.models import ProductImageVariant
        from apps.products.media import PUBLISHABLE_IMAGE_STATUSES
        from apps.products.models import ProductImage

        self._images_by_product = {}
        products = {}
        missing_product_ids = set()
        for listing in listings:
            product = listing.product
            product_id = getattr(product, 'pk', None)
            if product_id is None or product_id in products:
                continue
            products[product_id] = product
            prefetched = _prefetched_relation(product, 'images')
            if prefetched is _MISSING:
                missing_product_ids.add(product_id)
                continue
            publishable = [
                image for image in prefetched
                if image.status in PUBLISHABLE_IMAGE_STATUSES
            ]
            self._images_by_product[product_id] = sorted(
                publishable,
                key=lambda image: (
                    not image.is_primary,
                    image.position,
                    image.pk or 0,
                ),
            )[:10]

        for product_id in missing_product_ids:
            self._images_by_product[product_id] = []
        if missing_product_ids:
            ranked_images = (
                ProductImage.objects.filter(
                    product_id__in=missing_product_ids,
                    status__in=PUBLISHABLE_IMAGE_STATUSES,
                )
                .annotate(
                    _feed_rank=Window(
                        expression=RowNumber(),
                        partition_by=[F('product_id')],
                        order_by=[
                            F('is_primary').desc(),
                            F('position').asc(),
                            F('pk').asc(),
                        ],
                    ),
                )
                .filter(_feed_rank__lte=10)
                .order_by('product_id', '-is_primary', 'position', 'pk')
            )
            for image in ranked_images:
                self._images_by_product[image.product_id].append(image)

        images = [
            image
            for product_images in self._images_by_product.values()
            for image in product_images
        ]
        images_without_variants = [
            image for image in images
            if _prefetched_relation(image, 'variants') is _MISSING
        ]
        if images_without_variants:
            prefetch_related_objects(
                images_without_variants,
                Prefetch(
                    'variants',
                    queryset=ProductImageVariant.objects.filter(is_active=True).only(
                        'id', 'product_image_id', 's3_key', 'is_active',
                    ),
                ),
            )

    def images_for(self, product):
        product_id = getattr(product, 'pk', None)
        if product_id is None:
            prefetched = _prefetched_relation(product, 'images')
            return prefetched
        return self._images_by_product.get(product_id, _MISSING)


def _limit_marketplace_text(value, max_length: int) -> str:
    """Enforce adapter limits even for legacy or manually edited listings."""
    text = str(value or '').strip()
    if len(text) <= max_length:
        return text
    shortened = text[:max_length + 1]
    boundary = max(shortened.rfind('\n'), shortened.rfind(' '))
    if boundary >= int(max_length * 0.8):
        return shortened[:boundary].rstrip(' ,;:-')
    return text[:max_length].rstrip(' ,;:-')


def write_feed(
    listings,
    sink: BinaryIO,
    *,
    max_bytes: int | None = None,
) -> FeedWriteResult:
    """Write one exact Avito XML generation to a caller-owned binary sink.

    Relations are prepared in bounded batches and only one ``Ad`` element is
    materialized at a time.  The sink stays open and positioned after the last
    byte; private storage callers can therefore use a dedicated disk-backed
    file without creating a second full-payload ``bytes`` object in memory.
    """

    writer = _HashingBoundedWriter(sink, max_bytes=max_bytes)
    context = _FeedBuildContext()
    listing_count = 0
    writer.write(b"<?xml version='1.0' encoding='UTF-8'?>\n")
    writer.write(b'<Ads formatVersion="3" target="Avito.ru">\n')

    for batch in _batched(listings, _FEED_BUILD_BATCH_SIZE):
        context.prepare_batch(batch)
        for listing in batch:
            ad = _build_feed_ad(listing, context)
            # level=1 produces the same two/four-space indentation as the
            # former whole-document ElementTree, but only for one Ad.
            ET.indent(ad, space='  ', level=1)
            writer.write(b'  ')
            writer.write(ET.tostring(ad, encoding='UTF-8'))
            writer.write(b'\n')
            listing_count += 1

    writer.write(b'</Ads>')
    return FeedWriteResult(
        listing_count=listing_count,
        size_bytes=writer.size_bytes,
        payload_sha256=writer.payload_sha256,
    )


def build_feed(listings: list) -> bytes:
    """
    Генерирует XML-фид в формате Avito Autoload (formatVersion=3) для списка листингов.

    В фид попадают только активные объявления. Снятие с публикации в Avito
    делается ОТСУТСТВИЕМ объявления в файле (Avito архивирует то, чего нет),
    а не тегом — поэтому archived/deleted сюда передавать не нужно.
    Возвращает bytes в UTF-8 с XML-декларацией. Private artifact code должен
    использовать ``write_feed`` и disk-backed sink, чтобы не читать финальный
    payload целиком в RAM.
    """
    with tempfile.SpooledTemporaryFile(
        max_size=_FEED_SPOOL_MEMORY_BYTES,
        mode='w+b',
    ) as buf:
        write_feed(listings, cast(BinaryIO, buf))
        buf.seek(0)
        return buf.read()


def _build_feed_ad(listing, context: _FeedBuildContext):
    product = listing.product
    mapping = context.category_mapping(listing)
    spec = context.avito_spec(listing)
    category = context.category_for(listing)
    manual_address, bulk_address, account_address = context.placement_sources(listing)

    ad = ET.Element('Ad')
    # Id — наш ключ идемпотентности; по нему сопоставляем с avito_id через API
    ET.SubElement(ad, 'Id').text = str(listing.publish_idempotency_key)
    ET.SubElement(ad, 'Title').text = _limit_marketplace_text(
        listing.title or product.name or '', AVITO_TITLE_MAX_LENGTH,
    )
    ET.SubElement(ad, 'Description').text = _limit_marketplace_text(
        listing.description_ai or getattr(product, 'description_1c', '') or '',
        AVITO_DESCRIPTION_MAX_LENGTH,
    )
    ET.SubElement(ad, 'Price').text = str(int(listing.price_on_listing))
    ET.SubElement(ad, 'Category').text = _get_avito_category(
        listing, mapping=mapping, spec=spec,
    )
    # AdType / GoodsType — обязательные параметры категории «Запчасти и аксессуары».
    # Без них Avito отклоняет объявление при обработке фида (коды 1073/1123).
    ET.SubElement(ad, 'AdType').text = (
        getattr(listing, 'ad_type', '') or 'Товар приобретен на продажу'
    )
    ET.SubElement(ad, 'GoodsType').text = _get_goods_type(
        listing, mapping=mapping, spec=spec,
    )
    # ProductType («Тип товара») — обязателен для запчастей, но зависит от
    # конкретной детали, поэтому отдаём только если задан в маппинге категории.
    product_type = _get_product_type(listing, mapping=mapping, spec=spec)
    if product_type:
        ET.SubElement(ad, 'ProductType').text = product_type
    # SparePartType («Вид запчасти», напр. «Автосвет») — обязателен, но Avito
    # умеет определить его сам по названию; отдаём, если задан в маппинге.
    spare_part_type = _get_spare_part_type(listing, mapping=mapping, spec=spec)
    if spare_part_type:
        ET.SubElement(ad, 'SparePartType').text = spare_part_type
    # Под-вид (EngineSparePartType / BodySparePartType / …) — для категорий Avito,
    # где он обязателен (Двигатель/Кузов/Трансмиссия). Берём из дерева Avito.
    subtype_tag, subtype_value = _get_part_subtype(
        listing, spec=spec, category=category,
    )
    if subtype_tag and subtype_value:
        ET.SubElement(ad, subtype_tag).text = subtype_value
    # Необязательные Brand/OEM нельзя заполнять сомнительными fallback-значениями:
    # Avito валидирует даже переданный optional field и может отклонить весь Ad.
    brand = context.brand_for(listing, spec=spec)
    if brand:
        ET.SubElement(ad, 'Brand').text = brand
    oem = _get_oem(listing)
    if oem:
        ET.SubElement(ad, 'OEM').text = oem
    _add_placement(
        ad,
        listing,
        mapping=mapping,
        manual_address=manual_address,
        bulk_address=bulk_address,
        account_address=account_address,
    )
    ET.SubElement(ad, 'Condition').text = _CONDITION_MAP.get(
        getattr(product, 'condition', ''), 'Новое'
    )
    ET.SubElement(ad, 'AllowEmail').text = 'Нет'
    _add_images(
        ad,
        product,
        images=context.images_for(product),
        category=category,
    )
    return ad


def get_ad_id(listing) -> str:
    """Возвращает Id объявления в фиде — используется как ключ при сопоставлении с Avito."""
    return str(listing.publish_idempotency_key)


def build_stop_feed() -> bytes:
    """
    Возвращает спец-фид «STOP» — документированная команда Avito снять ВСЕ
    объявления аккаунта с публикации (когда активных не осталось).

    Формат: <Ads target="Avito.ru" formatVersion="3"><Ad><Id>STOP</Id></Ad></Ads>
    """
    root = ET.Element('Ads', formatVersion='3', target='Avito.ru')
    ET.SubElement(ET.SubElement(root, 'Ad'), 'Id').text = 'STOP'
    ET.indent(root, space='  ')
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding='UTF-8', xml_declaration=True)
    return buf.getvalue()


def _avito_spec(listing) -> dict:
    """
    Спецификация Avito (slug/fixed/required) по нашей категории товара.

    Резолвит catalog_category товара в лист Avito через category_map и берёт
    фиксированные значения полей и список обязательных полей из справочника.
    """
    category = getattr(listing.product, 'catalog_category', None)
    if not category:
        return {}
    return _resolve_avito_spec(category, lambda node: getattr(node, 'parent', None))


def _resolve_avito_spec(category, parent_of) -> dict:
    from apps.marketplaces.adapters.avito.category_map import (
        avito_spec_for, leaf_spec_by_name, leaf_spec_by_slug,
    )

    parent = parent_of(category)
    # 1) Базовая таксономия — через курируемый маппинг category_map.
    spec = avito_spec_for(getattr(category, 'name', ''), getattr(parent, 'name', ''))
    if spec:
        return spec
    # 2) Категория из импортированного дерева Avito — поднимаемся к листу по slug
    # (external_id). Slug уникален, имя — нет: «Тормозная система» есть и в
    # легковой, и в грузовой ветках, поиск по имени уводил товар в грузовую.
    by_slug = leaf_spec_by_slug()
    node = category
    while node is not None:
        leaf = by_slug.get(getattr(node, 'external_id', '') or '')
        if leaf:
            return {'slug': leaf.get('slug'), 'fixed': leaf.get('fixed', {}),
                    'required': leaf.get('required', [])}
        node = parent_of(node)
    # 3) Легаси-фолбэк для записей без external_id — по имени (с предпочтением
    # легковой ветки при коллизии имён).
    by_name = leaf_spec_by_name()
    node = category
    while node is not None:
        leaf = by_name.get(node.name)
        if leaf:
            return {'slug': leaf.get('slug'), 'fixed': leaf.get('fixed', {}),
                    'required': leaf.get('required', [])}
        node = parent_of(node)
    return {}


def _avito_fixed(listing, tag: str, *, spec=_MISSING) -> str:
    """Фиксированное значение поля Avito (tag) из маппинга категории, иначе пусто."""
    if spec is _MISSING:
        spec = _avito_spec(listing)
    return (spec.get('fixed') or {}).get(tag, '')


def _get_spare_part_type(listing, *, mapping=_MISSING, spec=_MISSING) -> str:
    """«Вид запчасти» (SparePartType): attributes_map тенанта → маппинг категории → пусто."""
    if mapping is _MISSING:
        mapping = _get_category_mapping(listing)
    attributes = getattr(mapping, 'attributes_map', {}) if mapping else {}
    return _first_value(
        attributes.get('SparePartType'),
        attributes.get('spare_part_type'),
        _avito_fixed(listing, 'SparePartType', spec=spec),
    )


def _get_avito_category(listing, *, mapping=_MISSING, spec=_MISSING) -> str:
    """Avito-категория: из спеки листа (fixed.Category) → маппинг → дефолт."""
    fixed_category = _avito_fixed(listing, 'Category', spec=spec)
    if fixed_category:
        return fixed_category
    if mapping is _MISSING:
        mapping = _get_category_mapping(listing)
    if mapping:
        return mapping.category_target
    return _DEFAULT_CATEGORY


def _get_part_subtype(
    listing,
    *,
    spec=_MISSING,
    category=_MISSING,
) -> tuple[str | None, str]:
    """
    Вид запчасти 2-го уровня (EngineSparePartType / BodySparePartType / …) и значение.

    Если у листа Avito есть под-вид (в required) и товар стоит НИЖЕ листа в дереве
    Avito, то значение под-вида = имя категории товара (напр. «Патрубки вентиляции»).
    """
    if spec is _MISSING:
        spec = _avito_spec(listing)
    sub_tag = next(
        (t for t in (spec.get('required') or []) if t.endswith('SparePartType') and t != 'SparePartType'),
        None,
    )
    if not sub_tag:
        return None, ''
    if category is _MISSING:
        category = getattr(listing.product, 'catalog_category', None)
    if not category:
        return sub_tag, ''
    from apps.marketplaces.adapters.avito.category_map import leaf_spec_by_slug
    # Товар на самом листе Avito — вид не выбран; ниже листа — это и есть вид.
    # На листе товар стоит, если slug (external_id) или имя категории совпадают
    # с найденным листом; сравнение с конкретным листом, а не со словарём всех
    # имён — имена листьев не уникальны между ветками.
    leaf = leaf_spec_by_slug().get(spec.get('slug') or '') or {}
    external_id = getattr(category, 'external_id', '') or ''
    if external_id == spec.get('slug') or (not external_id and category.name == leaf.get('name')):
        return sub_tag, ''
    return sub_tag, category.name


def _get_goods_type(listing, *, mapping=_MISSING, spec=_MISSING) -> str:
    """
    Возвращает «Вид товара» (GoodsType) для категории «Запчасти и аксессуары».

    Берёт значение из attributes_map маппинга категории, иначе — дефолт «Запчасти».
    """
    if mapping is _MISSING:
        mapping = _get_category_mapping(listing)
    attributes = getattr(mapping, 'attributes_map', {}) if mapping else {}
    return _first_value(
        attributes.get('GoodsType'),
        attributes.get('goods_type'),
        _avito_fixed(listing, 'GoodsType', spec=spec),
        'Запчасти',
    )


def _brand_is_required(listing, *, spec=_MISSING) -> bool:
    """Требует ли выбранный лист Avito поле Brand."""
    if spec is _MISSING:
        spec = _avito_spec(listing)
    return 'Brand' in (spec.get('required') or [])


def _brand_lookup(brand: str) -> dict:
    from apps.marketplaces.adapters.avito.brand_catalog import lookup_brand
    return lookup_brand(brand)


def _get_brand(listing, *, spec=_MISSING, lookup_cache=None) -> str:
    """
    Безопасное значение производителя для XML.

    Неизвестный бренд отправляется только когда поле действительно обязательно:
    preflight тогда заблокирует публикацию и покажет поле тенанту. В optional
    категории неизвестное значение пропускается, чтобы оно само не стало
    причиной отклонения. Имя организации вместо производителя не подставляется.
    """
    brand = str(getattr(listing.product, 'brand', '') or '').strip()
    if not brand:
        return ''
    if lookup_cache is not None and brand in lookup_cache:
        lookup = lookup_cache[brand]
    else:
        lookup = _brand_lookup(brand)
        if lookup_cache is not None:
            lookup_cache[brand] = lookup
    if lookup['known']:
        return str(lookup.get('canonical') or brand)
    return brand if _brand_is_required(listing, spec=spec) else ''


def _oem_values(listing) -> list[str]:
    """Непустые уникальные OEM-значения ровно в том виде, как хранит товар."""
    values = []
    for raw in (getattr(listing.product, 'oem_numbers', None) or []):
        value = str(raw).strip()
        if value and value not in values:
            values.append(value)
    return values


def _get_oem(listing) -> str:
    """
    Один однозначный OEM-номер, допустимый для XML Avito.

    Поле необязательное. Не подставляем артикул и не генерируем фиктивный OEM:
    у аналогов настоящего OEM может не быть. Несколько найденных номеров также
    не склеиваем — поле Avito имеет тип ``input`` и отклоняет запятые/пробелы.
    """
    values = _oem_values(listing)
    if len(values) != 1:
        return ''
    value = values[0]
    return value if value.isascii() and value.isalnum() else ''


def _get_product_type(listing, *, mapping=_MISSING, spec=_MISSING) -> str:
    """
    Возвращает «Тип товара» (ProductType): attributes_map тенанта → маппинг категории.

    Для запчастей это класс техники («Для автомобилей»), для шин/масел — конкретный
    тип («Легковые шины», «Моторные масла»). Если не задано — тег в фид не попадёт.
    """
    if mapping is _MISSING:
        mapping = _get_category_mapping(listing)
    attributes = getattr(mapping, 'attributes_map', {}) if mapping else {}
    return _first_value(
        attributes.get('ProductType'),
        attributes.get('product_type'),
        _avito_fixed(listing, 'ProductType', spec=spec),
    )


# Поля, которые builder формирует независимо от category mapping. Address имеет
# отдельный preflight по фактически разрешённому Address/SellerAddressID.
# CompatibleCars Avito проверенно определяет по названию/совместимости, поэтому
# отсутствие отдельного тега не считается локальной дырой.
_FEED_ALWAYS_OR_PROVIDER_INFERRED_TAGS = {
    'Id', 'Title', 'Description', 'Price', 'Category', 'GoodsType', 'AdType',
    'Condition', 'Address', 'CompatibleCars',
}


def missing_required_avito_fields(listing) -> list[str]:
    """
    Обязательные поля листа Avito, которые фид не заполняет (по справочнику категории).

    Используется для предупреждения тенанта: для таких товаров (напр. шины без
    размеров, масла без вязкости) Avito может отклонить объявление. Возвращает
    список тегов; пусто — если категория не сопоставлена или всё заполняется.
    """
    required = _avito_spec(listing).get('required') or []
    provided = set(_FEED_ALWAYS_OR_PROVIDER_INFERRED_TAGS)
    if _get_product_type(listing):
        provided.add('ProductType')
    if _get_spare_part_type(listing):
        provided.add('SparePartType')
    if str(getattr(listing.product, 'brand', '') or '').strip():
        provided.add('Brand')
    if _get_oem(listing):
        provided.add('OEM')
    subtype_tag, subtype_value = _get_part_subtype(listing)
    if subtype_tag and subtype_value:
        provided.add(subtype_tag)  # под-вид заполнен из дерева — не считаем дырой
    return [tag for tag in required if tag not in provided]


# Под-виды запчастей: обязательные поля, без которых Avito гарантированно
# отклоняет объявление («Не заполнен обязательный параметр …», code 1000).
# В карточке листинга это «Подкатегория 3» — вид детали ниже листа Avito.
AVITO_SUBTYPE_LABELS = {
    'TransmissionSparePartType': 'Тип детали трансмиссии',
    'EngineSparePartType': 'Тип детали двигателя',
    'BodySparePartType': 'Тип детали кузова',
    'TechnicSparePartType': 'Тип детали',
}

# Технические XML-теги Avito нельзя показывать тенанту без расшифровки.
# Справочник покрывает все обязательные теги из текущего avito_field_specs.json,
# которые MAP пока не формирует самостоятельно.
AVITO_FIELD_LABELS = {
    'ACEA': 'класс моторного масла по ACEA',
    'API': 'класс моторного масла по API',
    'ASTM': 'стандарт охлаждающей жидкости ASTM',
    'ATF': 'тип трансмиссионной жидкости ATF',
    'AccessorySubType': 'подтип аксессуара',
    'AccessoryType': 'тип аксессуара',
    'Age': 'возрастная группа',
    'AmplifierType': 'тип усилителя',
    'AndroidOS': 'версия Android',
    'AudioType': 'тип аудиоустройства',
    'Axles': 'количество осей',
    'BackRimDiameter': 'диаметр заднего диска',
    'BackTireAspectRatio': 'профиль задней шины',
    'BackTireSectionWidth': 'ширина задней шины',
    'BodySparePartType': 'тип кузовной детали',
    'BoreDiameter': 'диаметр отверстия',
    'BrushLength': 'длина щётки',
    'BrushType': 'тип щётки стеклоочистителя',
    'CPU': 'процессор',
    'CamsNumber': 'количество камер',
    'Capacity': 'ёмкость аккумулятора',
    'ChannelsNumber': 'количество каналов',
    'ClothingType': 'тип одежды',
    'Color': 'цвет',
    'CoolingType': 'тип охлаждения',
    'CoverType': 'тип чехла',
    'DCL': 'пусковой ток аккумулятора',
    'DOT': 'класс тормозной жидкости DOT',
    'Design': 'конструкция',
    'DeviceType': 'тип устройства',
    'DumpTruckFunction': 'назначение самосвала',
    'EngineSparePartType': 'тип детали двигателя',
    'EquipmentBrand': 'марка оборудования',
    'EquipmentType': 'тип оборудования',
    'FuelType': 'тип топлива',
    'Gender': 'назначение по полу',
    'GoodsSubType': 'подтип товара',
    'GrossVehicleWeight': 'разрешённая полная масса',
    'HeatingEquipmentType': 'тип отопительного оборудования',
    'HelmetType': 'тип шлема',
    'HydraulicDistributorType': 'тип гидрораспределителя',
    'HydraulicStroke': 'ход гидроцилиндра',
    'Impedance': 'сопротивление',
    'InstallationLocation': 'место установки',
    'InstallationType': 'способ установки',
    'Make': 'марка техники',
    'MatType': 'тип коврика',
    'Material': 'материал',
    'Model': 'модель',
    'MountingType': 'тип крепления',
    'NumberOfSections': 'количество секций',
    'OEMOil': 'допуск производителя автомобиля',
    'OtherDefects': 'другие повреждения',
    'PartsHindcarriageType': 'тип детали ходовой части',
    'Polarity': 'полярность аккумулятора',
    'ProductType': 'тип товара',
    'PowerType': 'тип питания',
    'ProductSubType': 'подтип товара',
    'ProtectionType': 'тип защиты',
    'Quantity': 'количество в комплекте',
    'RAM': 'объём оперативной памяти',
    'RMS': 'номинальная мощность',
    'RMSfour': 'мощность при сопротивлении 4 Ом',
    'RMStwo': 'мощность при сопротивлении 2 Ом',
    'ROM': 'объём встроенной памяти',
    'ResidualTread': 'остаточная глубина протектора',
    'Resolution': 'разрешение экрана или камеры',
    'RimBolts': 'количество крепёжных отверстий диска',
    'RimBoltsDiameter': 'диаметр расположения крепёжных отверстий',
    'RimDIA': 'диаметр центрального отверстия диска',
    'RimDiameter': 'диаметр диска',
    'RimOffset': 'вылет диска',
    'RimType': 'тип диска',
    'RimWidth': 'ширина диска',
    'RodDiameter': 'диаметр штока',
    'SAE': 'вязкость масла по SAE',
    'SecondBrushLength': 'длина второй щётки',
    'SparePartType': 'вид запчасти',
    'Set': 'состав комплекта',
    'ShoeType': 'тип обуви',
    'Size': 'размер',
    'Technic': 'вид техники',
    'TechnicAddOnType': 'тип навесного оборудования',
    'TechnicHeight': 'высота детали',
    'TechnicLength': 'длина детали',
    'TechnicSparePartType': 'тип детали спецтехники',
    'TechnicWidth': 'ширина детали',
    'TireAspectRatio': 'профиль шины',
    'TireRuptureQuantity': 'количество проколов протектора',
    'TireSectionWidth': 'ширина шины',
    'TireSideRepairQuantity': 'количество ремонтов боковины',
    'TireType': 'тип шины',
    'TransmissionSparePartType': 'тип детали трансмиссии',
    'TransportType': 'тип транспорта',
    'VehicleType': 'тип транспортного средства',
    'VoiceCoil': 'количество звуковых катушек',
    'Voltage': 'напряжение аккумулятора',
    'Volume': 'объём',
    'WheelAxle': 'ось установки колеса',
}

AVITO_FIELD_EXAMPLES = {
    'Voltage': 'например, 12 В',
    'Capacity': 'например, 60 А·ч',
    'DCL': 'например, 540 А',
    'Polarity': 'прямая или обратная',
    'TechnicLength': 'в миллиметрах',
    'TechnicWidth': 'в миллиметрах',
    'TechnicHeight': 'в миллиметрах',
}


def avito_field_label(tag: str) -> str:
    """Возвращает понятное пользователю название обязательной характеристики."""
    return AVITO_FIELD_LABELS.get(tag, 'дополнительная характеристика товара')


def format_avito_field_requirements(tags: list[str]) -> str:
    """Форматирует список полей без технических XML-тегов."""
    formatted = []
    for tag in tags:
        label = avito_field_label(tag)
        example = AVITO_FIELD_EXAMPLES.get(tag)
        formatted.append(f'{label} ({example})' if example else label)
    return '; '.join(formatted)


def blocking_missing_avito_fields(listing) -> list[str]:
    """Недостающие поля, из-за которых Avito гарантированно отклонит объявление.

    В отличие от остального required-списка (CompatibleCars и т.п. Avito умеет
    определять сам), под-вид детали реально блокирует публикацию — проверено
    отчётами автозагрузки.
    """
    return [tag for tag in missing_required_avito_fields(listing) if tag in AVITO_SUBTYPE_LABELS]


def product_brand_is_missing(listing) -> bool:
    """Пуст ли Brand именно в категории, где Avito помечает его required."""
    if not _brand_is_required(listing):
        return False
    return not str(getattr(listing.product, 'brand', '') or '').strip()


def unknown_brand_details(listing) -> tuple[str, list[str]] | None:
    """Неизвестный Brand в категории, где это поле обязательно.

    None — если Brand optional, известен каталогу, пуст (отдельная проверка)
    либо локальный каталог недоступен (fail-open).
    """
    if not _brand_is_required(listing):
        return None
    brand = str(getattr(listing.product, 'brand', '') or '').strip()
    if not brand:
        return None
    result = _brand_lookup(brand)
    if result['known']:
        return None
    return brand, result['suggestions']


def optional_unknown_brand_details(listing) -> tuple[str, list[str]] | None:
    """Неизвестный optional Brand, который builder безопасно не отправит."""
    if _brand_is_required(listing):
        return None
    brand = str(getattr(listing.product, 'brand', '') or '').strip()
    if not brand:
        return None
    result = _brand_lookup(brand)
    if result['known']:
        return None
    return brand, result['suggestions']


def _unknown_brand_warning(brand: str, suggestions: list[str]) -> str:
    hint = ''
    if suggestions:
        variants = ', '.join(f'«{suggestion}»' for suggestion in suggestions)
        hint = (
            f' В справочнике есть похожее название: {variants}. '
            'Выбирайте его только в том случае, если это действительно тот же производитель.'
        )
    return (
        f'Avito не распознал производителя «{brand}». В выбранной категории Brand '
        f'обязателен, поэтому объявление будет отклонено. Проверьте написание '
        f'в карточке товара.{hint} Если название указано верно, обратитесь в '
        f'поддержку Avito с просьбой добавить производителя в справочник.'
    )


def _optional_unknown_brand_warning(brand: str, suggestions: list[str]) -> str:
    hint = ''
    if suggestions:
        variants = ', '.join(f'«{suggestion}»' for suggestion in suggestions)
        hint = f' Похожие значения Avito: {variants}.'
    return (
        f'Avito не распознал необязательный бренд «{brand}». MAP не добавит его '
        f'в XML, чтобы необязательное значение не вызвало отклонение.{hint}'
    )


def _optional_oem_warning(listing) -> str:
    """Жёлтое пояснение, когда сохранённый optional OEM нельзя отправить."""
    values = _oem_values(listing)
    if not values or _get_oem(listing):
        return ''
    if len(values) > 1:
        preview = ', '.join(f'«{value}»' for value in values[:3])
        if len(values) > 3:
            preview += f' и ещё {len(values) - 3}'
        return (
            f'В источнике несколько OEM-номеров: {preview}. Поле Avito '
            'необязательное и принимает одно значение, поэтому MAP не добавит '
            'OEM в XML. Если номер нужно передать, оставьте в источнике один.'
        )
    return (
        f'OEM-номер «{values[0]}» содержит символы кроме латинских букв и цифр. '
        'Поле Avito необязательное, поэтому MAP не добавит его в XML. Исправьте '
        'номер в источнике, если его нужно передать.'
    )


def avito_publication_preflight(
    listing,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Return blocking errors and non-blocking warnings by drawer field.

    The checks intentionally reuse feed value resolvers. Required values are
    red blockers; optional or safely omitted values are yellow warnings. This
    keeps drawer, save API, publish API and XML generation on one contract.
    """

    product = listing.product
    errors: dict[str, list[str]] = {}
    warnings: dict[str, list[str]] = {}

    def add(target: dict[str, list[str]], field: str, message: str) -> None:
        target.setdefault(field, []).append(message)

    title = str(
        getattr(listing, 'title', '')
        or getattr(product, 'name', '')
        or ''
    ).strip()
    if not title:
        add(errors, 'title', 'Укажите заголовок объявления.')

    description = str(
        getattr(listing, 'description_ai', '')
        or getattr(product, 'description_1c', '')
        or ''
    ).strip()
    if not description:
        add(errors, 'description_ai', 'Добавьте описание объявления.')

    try:
        price_is_valid = float(getattr(listing, 'price_on_listing', 0) or 0) > 0
    except (TypeError, ValueError):
        price_is_valid = False
    if not price_is_valid:
        add(errors, 'price_on_listing', 'Цена объявления должна быть больше нуля.')

    account = getattr(listing, 'account', None)
    if (
        account is None
        or not getattr(account, 'is_active', False)
        or getattr(account, 'deleted_at', None) is not None
    ):
        add(errors, 'account_id', 'Выберите активный аккаунт Avito.')

    seller_address_id, address = get_placement_fields(listing)
    if not seller_address_id and not address:
        add(
            errors,
            'placement_address',
            'Выберите адрес размещения или укажите адрес в настройках аккаунта Avito.',
        )

    manager_name, contact_phone = get_contact_fields(listing)
    if not manager_name:
        add(
            errors,
            'manager_name_override',
            'Укажите контактное лицо в листинге, адресе размещения или настройках аккаунта Avito.',
        )
    if not contact_phone:
        add(
            errors,
            'contact_phone_override',
            'Укажите телефон в листинге, адресе размещения или настройках аккаунта Avito.',
        )

    if not has_resolved_category(listing):
        add(
            warnings,
            'catalog_category',
            'Не определена категория Avito с точным листом. MAP отправит общий '
            'тип запчастей, но не сможет заранее проверить все характеристики.',
        )

    brand = str(getattr(product, 'brand', '') or '').strip()
    brand_required = _brand_is_required(listing)
    brand_lookup = _brand_lookup(brand) if brand else None
    if brand_required and not brand:
        add(
            errors,
            'product_brand',
            'Для выбранной категории Avito производитель обязателен. Укажите бренд из справочника.',
        )
    elif brand_lookup is not None and not brand_lookup['known']:
        details = (brand, brand_lookup['suggestions'])
        if brand_required:
            add(errors, 'product_brand', _unknown_brand_warning(*details))
        else:
            add(
                warnings,
                'product_brand',
                _optional_unknown_brand_warning(*details),
            )

    oem_warning = _optional_oem_warning(listing)
    if oem_warning:
        add(warnings, 'product_oem', oem_warning)

    blocking = blocking_missing_avito_fields(listing)
    if blocking:
        labels = ', '.join(AVITO_SUBTYPE_LABELS[tag] for tag in blocking)
        add(
            errors,
            'catalog_category',
            f'Выберите конечную категорию Avito, чтобы заполнить: {labels}.',
        )

    other_required = [
        tag for tag in missing_required_avito_fields(listing)
        if tag not in AVITO_SUBTYPE_LABELS and tag != 'Brand'
    ]
    if other_required:
        requirements = format_avito_field_requirements(other_required)
        add(
            errors,
            'catalog_category',
            'Для выбранной категории Avito не заполнены обязательные '
            f'характеристики: {requirements}. Выберите точную категорию или '
            'добавьте эти характеристики перед публикацией.',
        )

    image_urls, uses_category_fallback = get_feed_image_urls(product)
    if not image_urls:
        add(
            warnings,
            'images',
            'Добавьте фотографию товара. Поле необязательное, поэтому публикация доступна.',
        )
    elif uses_category_fallback:
        add(
            warnings,
            'images',
            'Сейчас используется общая фотография категории. Добавьте фото конкретного товара.',
        )

    return errors, warnings


def avito_publication_field_errors(listing) -> dict[str, list[str]]:
    """Blocking preflight errors keyed by editable drawer field."""
    return avito_publication_preflight(listing)[0]


def avito_publication_field_warnings(listing) -> dict[str, list[str]]:
    """Non-blocking preflight warnings keyed by editable drawer field."""
    return avito_publication_preflight(listing)[1]


def avito_field_warnings(listing) -> list[str]:
    """Плоский compatibility-список жёлтых preflight-предупреждений."""
    warnings_by_field = avito_publication_field_warnings(listing)
    return [message for messages in warnings_by_field.values() for message in messages]


def get_contact_fields(
    listing,
    *,
    mapping=_MISSING,
    manual_address=_MISSING,
    bulk_address=_MISSING,
    account_address=_MISSING,
) -> tuple[str, str]:
    """
    Возвращает (контактное лицо, телефон) по тем же приоритетам, что и фид.

    Используется фидом и валидацией публикации, чтобы не отправлять в Avito
    объявления с пустыми контактами.
    """
    if mapping is _MISSING:
        mapping = _get_category_mapping(listing)
    attributes = getattr(mapping, 'attributes_map', {}) if mapping else {}
    account = listing.account
    if manual_address is _MISSING:
        manual_address = getattr(listing, 'placement_address', None)
    if bulk_address is _MISSING:
        bulk_address = getattr(listing, 'bulk_placement_address', None)
    if account_address is _MISSING:
        account_address = _get_account_default_address(account)

    manager_name = _first_value(
        getattr(manual_address, 'manager_name', ''),
        getattr(listing, 'manager_name_override', ''),
        getattr(bulk_address, 'manager_name', ''),
        getattr(listing, 'bulk_manager_name', ''),
        attributes.get('manager_name'),
        attributes.get('ManagerName'),
        getattr(account_address, 'manager_name', ''),
        getattr(account, 'default_manager_name', ''),
    )
    contact_phone = _first_value(
        getattr(manual_address, 'contact_phone', ''),
        getattr(listing, 'contact_phone_override', ''),
        getattr(bulk_address, 'contact_phone', ''),
        getattr(listing, 'bulk_contact_phone', ''),
        attributes.get('contact_phone'),
        attributes.get('ContactPhone'),
        getattr(account_address, 'contact_phone', ''),
        getattr(account, 'default_contact_phone', ''),
    )
    return manager_name, contact_phone


def _get_category_mapping(listing):
    try:
        from apps.marketplaces.models import CategoryMapping
        # Приоритет — категория из источника (1С). Если по ней маппинга нет,
        # пробуем по имени категории каталога: импортированные из дерева Avito
        # маппинги ключуются именно по имени листа (см. AvitoCatalogImporter).
        candidates = []
        if listing.product.category_1c:
            candidates.append(listing.product.category_1c)
        catalog_category = getattr(listing.product, 'catalog_category', None)
        if catalog_category is not None:
            candidates.append(catalog_category.name)
        prefetched = _prefetched_relation(listing.tenant, 'category_mappings')
        if prefetched is not _MISSING:
            by_source = {
                mapping.category_source: mapping
                for mapping in prefetched
                if mapping.marketplace == CategoryMapping.MARKETPLACE_AVITO
            }
            for source in candidates:
                mapping = by_source.get(source)
                if mapping is not None:
                    return mapping
            return None

        qs = CategoryMapping.objects.filter(
            tenant=listing.tenant,
            marketplace=CategoryMapping.MARKETPLACE_AVITO,
        )
        for source in candidates:
            mapping = qs.filter(category_source=source).first()
            if mapping:
                return mapping
        return None
    except Exception:
        return None


def has_resolved_category(listing) -> bool:
    """
    True, если у листинга определена категория для Avito.

    Категория считается определённой, только если catalog_category реально
    разрешается в Avito leaf spec либо найден legacy CategoryMapping. Само
    наличие произвольной локальной категории не доказывает корректный feed.
    """
    if _avito_spec(listing):
        return True
    return _get_category_mapping(listing) is not None


def _add_placement(
    ad,
    listing,
    *,
    mapping=_MISSING,
    manual_address=_MISSING,
    bulk_address=_MISSING,
    account_address=_MISSING,
) -> None:
    """Добавляет адрес и контактные поля из листинга, категории или аккаунта."""
    seller_address_id, address = get_placement_fields(
        listing,
        mapping=mapping,
        manual_address=manual_address,
        bulk_address=bulk_address,
        account_address=account_address,
    )
    manager_name, contact_phone = get_contact_fields(
        listing,
        mapping=mapping,
        manual_address=manual_address,
        bulk_address=bulk_address,
        account_address=account_address,
    )

    if seller_address_id:
        ET.SubElement(ad, 'SellerAddressID').text = seller_address_id
    elif address:
        ET.SubElement(ad, 'Address').text = address
    if manager_name:
        ET.SubElement(ad, 'ManagerName').text = manager_name
    if contact_phone:
        ET.SubElement(ad, 'ContactPhone').text = contact_phone


def get_placement_fields(
    listing,
    *,
    mapping=_MISSING,
    manual_address=_MISSING,
    bulk_address=_MISSING,
    account_address=_MISSING,
) -> tuple[str, str]:
    """Возвращает фактические ``(SellerAddressID, Address)`` из цепочки фида."""
    if mapping is _MISSING:
        mapping = _get_category_mapping(listing)
    attributes = getattr(mapping, 'attributes_map', {}) if mapping else {}
    account = getattr(listing, 'account', None)
    if manual_address is _MISSING:
        manual_address = getattr(listing, 'placement_address', None)
    if bulk_address is _MISSING:
        bulk_address = getattr(listing, 'bulk_placement_address', None)
    if account_address is _MISSING:
        account_address = _get_account_default_address(account)

    seller_address_id = _first_value(
        getattr(manual_address, 'seller_address_id', ''),
        getattr(listing, 'seller_address_id_override', ''),
        getattr(bulk_address, 'seller_address_id', ''),
        getattr(listing, 'bulk_seller_address_id', ''),
        attributes.get('seller_address_id'),
        attributes.get('SellerAddressID'),
        getattr(account_address, 'seller_address_id', ''),
        getattr(account, 'default_seller_address_id', ''),
    )
    address = _first_value(
        getattr(manual_address, 'address', ''),
        getattr(listing, 'address_override', ''),
        getattr(bulk_address, 'address', ''),
        getattr(listing, 'bulk_address', ''),
        attributes.get('address'),
        attributes.get('Address'),
        getattr(account_address, 'address', ''),
        getattr(account, 'default_address', ''),
    )
    # Защита от мусорного значения: external_id аккаунта — это не ID адреса.
    # Avito такой SellerAddressID не находит, поэтому игнорируем и шлём текстовый адрес.
    if seller_address_id and seller_address_id == str(getattr(account, 'external_id', '') or ''):
        seller_address_id = ''
    return seller_address_id, address


def _get_account_default_address(account):
    if not account:
        return None
    try:
        prefetched = _prefetched_relation(account, 'placement_addresses')
        if prefetched is not _MISSING:
            defaults = sorted(
                (
                    address for address in prefetched
                    if address.is_active and address.is_default
                ),
                key=lambda address: (address.name, address.pk),
            )
            return defaults[0] if defaults else None
        return account.placement_addresses.filter(is_active=True, is_default=True).first()
    except Exception:
        return None


def _first_value(*values) -> str:
    for value in values:
        text = str(value or '').strip()
        if text:
            return text
    return ''


def _add_images(ad, product, *, images=_MISSING, category=_MISSING) -> None:
    """Добавляет в фид только одобренные/ручные/импортированные фото."""
    urls, _uses_category_fallback = get_feed_image_urls(
        product,
        images=images,
        category=category,
    )
    if not urls:
        return

    images_node = ET.SubElement(ad, 'Images')
    for url in urls:
        ET.SubElement(images_node, 'Image', url=url)


def get_feed_image_urls(
    product,
    *,
    images=_MISSING,
    category=_MISSING,
) -> tuple[list[str], bool]:
    """Возвращает те же URL, что XML, и признак category fallback."""
    from apps.products.media import (
        get_product_image_delivery_key, get_publishable_product_images,
    )

    if images is _MISSING:
        images = get_publishable_product_images(product)
    urls = []
    for image in images:
        url = _image_url(get_product_image_delivery_key(image), image.url_source)
        if url.startswith('http'):
            urls.append(url)

    uses_category_fallback = False
    if not urls:
        if category is _MISSING:
            category = getattr(product, 'catalog_category', None)
        if category and category.default_image_s3_key:
            url = _image_url(category.default_image_s3_key, '')
            if url.startswith('http'):
                urls.append(url)
                uses_category_fallback = True
    return urls[:10], uses_category_fallback


def _image_url(s3_key: str, fallback: str) -> str:
    """Строит публичный URL изображения для фида."""
    from django.core.files.storage import default_storage

    cdn = getattr(settings, 'YC_CDN_DOMAIN', '')
    is_s3 = hasattr(default_storage, 'bucket_name')
    if cdn and s3_key and is_s3:
        return f'https://{cdn}/{s3_key}'
    if s3_key:
        return default_storage.url(s3_key)
    return fallback or ''
