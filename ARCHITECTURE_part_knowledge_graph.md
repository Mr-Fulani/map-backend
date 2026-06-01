# ARCHITECTURE: Platform Parts Knowledge Graph

> **Дата:** 29.05.2026
> **Проект:** MAP
> **Статус:** первая итерация после enrichment MVP
> **Цель:** переиспользовать справочные знания о запчастях между tenant-ами без смешивания коммерческих данных tenant-ов.

## Зачем нужен граф

Enrichment-парсер обогащает каталог tenant-а характеристиками, OEM/Cross-кодами,
применяемостью, фактами для AI-описания и ссылками на изображения. Но часть этих
данных полезна не только одному tenant-у:

- один и тот же артикул может встречаться у разных tenant-ов;
- OEM/Cross-связи помогают искать аналоги и уточнять применяемость;
- источник может не найти карточку по прямому URL, но платформа уже могла узнать
  связь из другого товара;
- AI-агенту нужны конкретные факты, а не фразы вроде "подходит для разных авто".

Поэтому вводится platform-level слой `GlobalPart` и `GlobalPartRelation`.

## Что хранится глобально

`GlobalPart` — нормализованный справочный артикул без tenant-коммерции.

Хранится:

- бренд/производитель;
- нормализованный бренд;
- артикул;
- нормализованный артикул;
- название из источника, если есть;
- источник и URL;
- confidence;
- `needs_review`;
- `last_seen_at`.

Не хранится:

- цена;
- остаток;
- склад;
- tenant-владение;
- marketplace/listing-статусы.

`GlobalPartRelation` — связь между двумя `GlobalPart`.

Типы связи:

- `OEM` — оригинальный номер;
- `Cross` — кросс-код;
- `Analogue` — аналог;
- `Replacement` — заменитель;
- `Trade` — торговый номер;
- `Unknown` — связь найдена, но тип неясен и нужна проверка.

`GlobalPartFitment` — доказанная применяемость глобального артикула к автомобилю,
полученная из источника, который явно отдал fitment-данные.

`VehicleMake`, `VehicleModel` и `VehicleGeneration` — нормализованный справочник
авто. Raw строки `make/model/generation` в `GlobalPartFitment` остаются
источником правды для отладки, а FK на справочник добавляются только когда
сопоставление уверенное.

`PartCategory` — легкий platform-level справочник категорий автозапчастей. Он
нужен для будущих правил применяемости, фильтрации и AI-контекста, но не заменяет
tenant-поле `Product.category_1c`.

Базовая таксономия автозапчастей хранится в `apps/products/part_category_seed.py`
и применяется миграцией `products.0017_seed_base_part_categories`. Это собственная
компактная структура MAP по общим рыночным группам автозапчастей, а не копия
TecDoc, Autodoc, Exist или другого поставщика.

Базовые корневые категории и стартовые tenant-шаблоны подкатегорий хранятся в
`apps/products/catalog_category_seed.py`. Миграция `tenants.0008` добавляет
популярные корни платформы, а `products.0019` сажает подкатегории для уже
включенных корней tenant-а. При включении корня в dashboard сервис создает
tenant-scoped копию шаблона в `TenantCatalogCategory`, чтобы tenant мог дальше
редактировать свои подкатегории без влияния на других.

Важно разделять уровни:

- `CatalogDomain` / `ProductCatalogClassification.domain` — platform guardrail.
- `PartCategory` — platform taxonomy автозапчастей.
- `ProductBrand` / `ProductBrandAlias` — platform-нормализация брендов без замены сырого бренда товара.
- `TenantCatalogCategory` — будущие редактируемые категории конкретного tenant-а.
- `Product.category_1c` — сырое поле из источника tenant-а, например 1С.

Эти сущности нельзя сливать в одну таблицу: у них разные владельцы, жизненный цикл
и последствия ошибок.

`Product.brand` и `GlobalPart.brand` остаются исходными строками из источника.
`Product.brand_ref` и `GlobalPart.brand_ref` — дополнительная ссылка на
нормализованный бренд, если его удалось уверенно определить. Для автозапчастей
ключ применяемости не меняется: граф по-прежнему ищет деталь по
`GlobalPart.normalized_brand + GlobalPart.normalized_article`, чтобы не сломать
связи OEM/cross/fitment между tenant-ами.

Важно: аналог или cross-код не равен доказанной применяемости. Это полезная связь
для поиска и обогащения, но конкретные марки/модели авто должны приходить из
`VehicleFitment` или `GlobalPartFitment`.

## Как работает поток данных

```text
Tenant Product
  -> ProductParseJob
  -> Part parser
  -> ParsedPart
  -> ProductEnrichmentService.save_parsed_part()
  -> tenant-scoped enrichment tables
  -> ProductKnowledgeGraphService.learn_from_parsed_part()
  -> GlobalPart / GlobalPartRelation / GlobalPartFitment
```

Tenant-scoped таблицы остаются источником данных для карточки конкретного товара:

- `ProductAttribute`;
- `ProductCrossCode`;
- `VehicleFitment`;
- `ProductEnrichmentFact`;
- `ProductImage`;
- denormalized поля `Product.oem_numbers`, `Product.cross_numbers`, `Product.applicability`.

Глобальный граф используется как справочная память платформы:

```text
ProductKnowledgeGraphService.apply_known_relations_to_product(product)
  -> находит GlobalPart по brand/article
  -> берет outgoing GlobalPartRelation
  -> создает ProductCrossCode для конкретного tenant/product
  -> обновляет Product.oem_numbers / Product.cross_numbers

ProductKnowledgeGraphService.apply_known_fitments_to_product(product)
  -> находит GlobalPart по brand/article
  -> берет trusted GlobalPartFitment
  -> создает VehicleFitment для конкретного tenant/product
  -> обновляет Product.applicability
```

Так tenant B может получить уже известные OEM/Cross-связи и применяемость,
которые tenant A получил через успешный парсинг, но данные всё равно копируются
в tenant-scoped таблицы конкретного товара.

## Правила безопасности данных

- Глобальный граф не перезаписывает цену, остаток, склад и коммерческие поля.
- Повторное обучение не удаляет старые полезные связи.
- Новые значения добавляются через `get_or_create`/merge-подход.
- Более высокий confidence может поднять уверенность связи.
- Если тип связи неизвестен, запись получает `needs_review=True`.
- Данные из графа применяются к tenant-товару только как enrichment-копия,
  чтобы не смешивать tenant-данные на уровне UI/API.
- Автоматическое применение проходит через `source_policy`: источник должен быть
  известен, запись не должна требовать review, confidence должен быть выше порога.

## Source Quality Policy

`apps/products/source_policy.py` — единое место для правил источников.

Сейчас policy хранит:

- `source_id`;
- человекочитаемый `label`;
- `priority` и `trust_score`;
- batch/rate-limit параметры;
- transport (`httpx` по умолчанию);
- capabilities источника;
- `auto_apply_min_confidence`;
- `auto_apply_min_trust_score`.

Для `tachka` включены product page, search fallback, fitments, images и related
parts. Это не означает, что данные источника всегда истинны: конкретная запись
все равно проходит проверку `needs_review`, `confidence` и типа связи.

Правила auto-apply:

```text
review_status == rejected -> never apply
review_status == approved -> apply as operator-confirmed data

relation.needs_review == false
relation.relation_type != Unknown
source.trust_score >= source.auto_apply_min_trust_score
relation.confidence >= source.auto_apply_min_confidence

fitment.needs_review == false
fitment.model is not empty
source.trust_score >= source.auto_apply_min_trust_score
fitment.confidence >= source.auto_apply_min_confidence
```

Operator review хранится рядом с фактом: `review_status`, `reviewed_at`,
`reviewed_by`. Для tenant-scoped данных это есть у `ProductCatalogClassification`,
`VehicleFitment` и `ProductEnrichmentFact`. Отклонённая применяемость остаётся с
provenance, но не попадает в `Product.applicability`.

Fetcher/transport отделен от HTML parser logic:

- `apps/products/part_fetchers.py` содержит `HttpxPartFetcher`;
- `TachkaPartParser` получает fetcher через `__init__`;
- `get_part_fetcher(source_id)` выбирает transport по `source_policy`;
- parser можно тестировать на HTML fixtures или fake fetcher без сети.

`CloakBrowser` и похожие browser/stealth runtimes должны подключаться только как
optional transport для конкретного source policy. Они не должны попадать в core
parser как обязательная зависимость.

## Где смотреть в коде

- `apps/tenants/models.py` — `Tenant.catalog_domain` и capability для auto-parts enrichment.
- `apps/products/models.py` — `GlobalPart`, `GlobalPartRelation`, `PartCategory`.
- `apps/products/part_fetchers.py` — transport/fetcher слой для parser sources.
- `apps/products/services.py` — `ProductKnowledgeGraphService`.
- `apps/products/admin.py` — read-only admin для глобальных артикулов и связей.
- `apps/products/tests/test_knowledge_graph.py` — сценарии обучения и применения.

## Что это решает сейчас

- Платформа начинает накапливать знания по артикулам между tenant-ами.
- Повторный tenant с тем же `brand + article` может получить уже известные OEM/Cross.
- Повторный tenant с тем же `brand + article` может получить уже известную
  применяемость без внешнего fetch.
- Оператор может видеть глобальные связи в Django admin.
- Сомнительные связи не маскируются под достоверные.

## Search fallback и аналоги

После первой итерации графа добавлен fallback для случаев, когда прямой URL
карточки источника не найден:

```text
direct product URL
  -> PartNotFound
  -> source search by article/OEM
  -> parse search result groups
  -> ParsedRelatedPart[]
  -> GlobalPartRelation
```

Это закрывает важный сценарий: аналоги могут отсутствовать на странице конкретного
товара, но быть в поисковой выдаче или блоках "Аналоги по OEM". Parser не обязан
обходить весь каталог. Он делает точечный поиск по артикулу/OEM в рамках текущего
enrichment job и сохраняет только найденные структурные связи.

В коде это разделено так:

- `TachkaPartParser.parse_html()` — карточка товара.
- `TachkaPartParser.parse_search_html()` — поисковая выдача/аналоги.
- `ParsedRelatedPart` — связанный артикул без обещания применяемости.
- `ProductKnowledgeGraphService.learn_from_parsed_part()` — сохранение связей в
  `GlobalPartRelation`.

Производительность и безопасность:

- fallback запускается только после `PartNotFound` прямой карточки;
- массовые запуски продолжают идти через `ProductBulkActionJob` batch/cooldown;
- глобальный граф использует нормализованные ключи и индексы;
- `Unknown`/`needs_review` связи не применяются автоматически к tenant-товару;
- аналоги не создают `VehicleFitment`, пока нет явных данных применяемости.
- trusted `GlobalPartFitment` применяется до внешнего fetch, снижая нагрузку на источники.

## Catalog domain guardrail

Автозапчастное обогащение включается только для tenant-ов с:

```text
Tenant.catalog_domain = auto_parts
```

Для tenant-ов `generic`, `jewellery`, `apparel` и `other` запрещены:

- parse/enrichment job автозапчастей;
- массовые действия enrichment/OEM/fitment;
- попытки запускать parser перед генерацией описания.

При этом обычные сценарии платформы остаются доступными:

- импорт и редактирование `Product`;
- AI-генерация описания по базовым данным;
- поиск и загрузка фотографий.

Это важно для SaaS: неавтомобильный tenant не должен попадать в автомобильные
предметные правила, а auto-parts tenant сохраняет текущий enrichment pipeline.

Для tenant-а со смешанным ассортиментом используется:

```text
Tenant.catalog_domain = mixed
```

В этом режиме платформа разрешает auto-parts capability, но перед запуском parser
проверяет конкретный `Product` через `ProductCatalogClassification`.

`ProductCatalogClassification` хранит:

- `domain`: auto_parts/generic/jewellery/apparel/unknown;
- `confidence`;
- `source`: rules/manual/ai;
- `reason`;
- `needs_review`.

Если товар не классифицирован как `auto_parts` с достаточной уверенностью,
одиночный parser-запуск отклоняется, regenerate переходит в обычную AI-генерацию,
а bulk job пропускает такой товар и увеличивает `skipped_count`.

Для предварительного заполнения классификации используется bulk action
`classify_catalog_domain`. Он не обращается к внешним источникам и не создает
`ProductParseJob`, поэтому подходит для безопасной обработки уже импортированного
каталога перед запуском enrichment.

## Категории tenant-а и platform domains

Практичная модель для следующего этапа:

```text
TenantCatalogCategory
  -> tenant
  -> parent
  -> name
  -> domain
  -> aliases
  -> external_source/external_id
```

Tenant может редактировать свои категории, но не platform domains. Domain остается
контролируемым слоем платформы, потому что от него зависит запуск parser-ов и
domain-specific enrichment.

MVP tenant-категорий реализует:

- `TenantCatalogCategory`;
- `TenantCategoryMapping`;
- nullable `Product.catalog_category`;
- API и dashboard-вкладку `Настройки -> Категории`;
- использование mapping как сигнала для `ProductCatalogClassification`.

Правильный поток:

```text
Product.category_1c
  -> mapping to TenantCatalogCategory
  -> category domain as classification signal
  -> ProductCatalogClassification
  -> enrichment guardrail
```

Неправильный поток:

```text
Product.category_1c напрямую запускает auto-parts parser
tenant создает произвольный domain
TenantCatalogCategory заменяет PartCategory
PartCategory перетирает категорию tenant-а
```

Конфликты нужно решать через confidence/reason/needs_review. Если tenant category
говорит `auto_parts`, а название товара похоже на украшение, классификация должна
уйти в `needs_review`, а parser не должен запускаться автоматически.

## Что не решено в этой итерации

- Нормализованный справочник `VehicleModification`.
- Массовое назначение tenant-категорий товарам.
- Редактирование mappings в dashboard beyond MVP.
- Отдельная очередь operator review для больших объёмов спорных данных.
- Подключение второго enrichment source поверх source quality policy.
- UI для просмотра глобального графа в tenant dashboard.
- Поиск товаров tenant-а по марке/модели/поколению авто.

## Следующий логичный шаг

Следующий P0 — Vehicle Knowledge Base v2 для накопления применяемости и AI-
описаний. Нужно не начинать с пользовательского поиска по авто, а усилить поток:
товар tenant-а -> global graph -> parser при нехватке данных -> trusted факты для
AI.

```text
Product brand/article/name
  -> GlobalPart by normalized_brand + normalized_article
  -> trusted GlobalPartFitment / OEM / cross / analogue relations
  -> tenant VehicleFitment + enrichment facts
  -> AI description context
```

Raw строки применяемости остаются всегда. Нормализованные FK добавляются только
когда сопоставление уверенное; сомнительные поколения/модификации должны уходить
в review, а не перетирать полезную применяемость.

Поиск товаров по авто остается следующим продуктовым слоем после того, как база
применяемости достаточно наполняется и агент получает достоверные факты.

Это позволит не только хранить строки применяемости, но и устойчиво фильтровать,
нормализовать и разрешать конфликты между несколькими источниками.
