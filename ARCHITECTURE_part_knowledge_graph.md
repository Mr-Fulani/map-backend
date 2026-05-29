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

`VehicleMake` и `VehicleModel` — нормализованный справочник авто. Raw строки
`make/model` в `GlobalPartFitment` остаются источником правды для отладки, а FK
на справочник добавляются только когда сопоставление уверенное.

`PartCategory` — легкий platform-level справочник категорий автозапчастей. Он
нужен для будущих правил применяемости, фильтрации и AI-контекста, но не заменяет
tenant-поле `Product.category_1c`.

Важно разделять уровни:

- `CatalogDomain` / `ProductCatalogClassification.domain` — platform guardrail.
- `PartCategory` — platform taxonomy автозапчастей.
- `TenantCatalogCategory` — будущие редактируемые категории конкретного tenant-а.
- `Product.category_1c` — сырое поле из источника tenant-а, например 1С.

Эти сущности нельзя сливать в одну таблицу: у них разные владельцы, жизненный цикл
и последствия ошибок.

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
- `auto_apply_min_confidence`.

Для `tachka` включены product page, search fallback, fitments, images и related
parts. Это не означает, что данные источника всегда истинны: конкретная запись
все равно проходит проверку `needs_review`, `confidence` и типа связи.

Правила auto-apply:

```text
relation.needs_review == false
relation.relation_type != Unknown
relation.confidence >= source.auto_apply_min_confidence

fitment.needs_review == false
fitment.model is not empty
fitment.confidence >= source.auto_apply_min_confidence
```

`CloakBrowser` и похожие browser/stealth runtimes должны подключаться только как
optional transport для конкретного source policy. Они не должны попадать в core
parser как обязательная зависимость.

## Где смотреть в коде

- `apps/tenants/models.py` — `Tenant.catalog_domain` и capability для auto-parts enrichment.
- `apps/products/models.py` — `GlobalPart`, `GlobalPartRelation`, `PartCategory`.
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

- Нормализованный справочник `VehicleGeneration/VehicleModification`.
- Tenant-scoped категории каталога и маппинг `category_1c`.
- Merge-policy для ручной классификации vs rules-based классификации.
- Приоритеты нескольких источников.
- Правила конфликтов между источниками.
- UI для просмотра глобального графа в tenant dashboard.

## Следующий логичный шаг

Сначала добавить tenant-scoped категории и merge-policy классификации, затем
source priority и conflict resolution:

```text
Product.category_1c -> TenantCatalogCategory -> ProductCatalogClassification
manual classification policy
source fitment strings
  -> source priority/conflict rules
  -> safer GlobalPartFitment approval
```

Это позволит не только хранить строки применяемости, но и устойчиво фильтровать,
нормализовать и разрешать конфликты между несколькими источниками.
