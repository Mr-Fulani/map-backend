# ARCHITECTURE: Platform Parts Knowledge Graph

> **Дата:** 28.05.2026  
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

Важно: аналог или cross-код не равен доказанной применяемости. Это полезная связь
для поиска и обогащения, но конкретные марки/модели авто должны приходить из
`VehicleFitment` или будущего vehicle knowledge base.

## Как работает поток данных

```text
Tenant Product
  -> ProductParseJob
  -> Part parser
  -> ParsedPart
  -> ProductEnrichmentService.save_parsed_part()
  -> tenant-scoped enrichment tables
  -> ProductKnowledgeGraphService.learn_from_parsed_part()
  -> GlobalPart / GlobalPartRelation
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
```

Так tenant B может получить уже известные OEM/Cross-связи, которые tenant A
получил через успешный парсинг, но данные всё равно копируются в tenant-scoped
таблицы конкретного товара.

## Правила безопасности данных

- Глобальный граф не перезаписывает цену, остаток, склад и коммерческие поля.
- Повторное обучение не удаляет старые полезные связи.
- Новые значения добавляются через `get_or_create`/merge-подход.
- Более высокий confidence может поднять уверенность связи.
- Если тип связи неизвестен, запись получает `needs_review=True`.
- Данные из графа применяются к tenant-товару только как enrichment-копия,
  чтобы не смешивать tenant-данные на уровне UI/API.

## Где смотреть в коде

- `apps/products/models.py` — `GlobalPart`, `GlobalPartRelation`.
- `apps/products/services.py` — `ProductKnowledgeGraphService`.
- `apps/products/admin.py` — read-only admin для глобальных артикулов и связей.
- `apps/products/tests/test_knowledge_graph.py` — сценарии обучения и применения.

## Что это решает сейчас

- Платформа начинает накапливать знания по артикулам между tenant-ами.
- Повторный tenant с тем же `brand + article` может получить уже известные OEM/Cross.
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

## Что не решено в этой итерации

- Глобальная применяемость `part -> vehicle`.
- Нормализованный справочник `VehicleMake/VehicleModel/VehicleGeneration`.
- Приоритеты нескольких источников.
- Правила конфликтов между источниками.
- UI для просмотра глобального графа в tenant dashboard.

## Следующий логичный шаг

Добавить глобальную применяемость поверх уже найденных связей:

```text
GlobalPart / GlobalPartRelation
  -> source fitment data
  -> GlobalPartFitment / Vehicle KB
  -> tenant VehicleFitment on trusted match
```

Это позволит не только знать, что `485108Z460` связан с аналогами, но и безопасно
показывать конкретные марки/модели авто, когда источник действительно дал
применяемость.
