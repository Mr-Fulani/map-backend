# ROADMAP v1: Умный парсинг автозапчастей и применяемости

> **Проект:** MAP
> **Версия:** 1.2
> **Оценка:** 6-8 недель на production-ready MVP с запасом на разбор верстки источника
> **Подход:** сначала надежное enrichment-ядро без коммерческих данных, потом масштабирование и AI
> **Multi-tenant:** фича общая для платформы, но результаты записываются в каталог конкретного tenant
> **Статус на 31.05.2026:** enrichment MVP, первая версия platform knowledge graph, tenant-категории MVP и source quality policy реализованы; в работе P1 operator review workflow, затем нормализованный справочник авто

---

## Обзор фаз

```text
PHASE 0      PHASE 1      PHASE 2      PHASE 3      PHASE 4      PHASE 5      PHASE 6      PHASE 7
Discovery    Data Model   Parser Core  Save/Celery Admin/API    Quality/AI   Scale       Vehicle KB
2-3 дня      1 неделя     2 недели     1 неделя     1 неделя    1 неделя    1-2 недели  1-2 недели
```

## Текущая стадия реализации

На 28.05.2026 проект находится между `PHASE 6` и `PHASE 7`.

Уже реализовано:

- `[x]` tenant-scoped модели enrichment: `ProductAttribute`, `ProductCrossCode`, `VehicleFitment`, `ProductEnrichmentFact`, `ProductParseJob`.
- `[x]` модель массовых задач `ProductBulkActionJob`.
- `[x]` `TachkaPartParser` как первый источник.
- `[x]` сохранение характеристик, OEM/Cross, применяемости, facts и raw/parsed данных.
- `[x]` merge-first стратегия: полезные данные не удаляются при повторном обогащении из источника.
- `[x]` Celery-задачи `parse_single_part`, `parse_single_part_then_generate_description`, `process_bulk_product_action`.
- `[x]` очереди `part_parsing` и `part_parsing_bulk`.
- `[x]` API запуска/статуса parse job.
- `[x]` API массовых действий.
- `[x]` отображение enrichment status в каталоге.
- `[x]` карточка товара показывает характеристики, OEM/Cross, применяемость и source image URLs.
- `[x]` единая кнопка `Обогатить и сгенерировать`.
- `[x]` агент получает enrichment context и не должен писать размытую применяемость.
- `[x]` bulk actions работают batch-ами с паузой/cooling_down.
- `[x]` первая итерация platform-level `GlobalPart` / `GlobalPartRelation` для переиспользования OEM/Cross-связей между tenant-ами.
- `[x]` read-only Django admin для просмотра глобальных артикулов и связей.
- `[x]` сервис применения уже известных глобальных связей к tenant-scoped `ProductCrossCode`.
- `[x]` fallback на поисковую выдачу источника при `not_found` прямой карточки.
- `[x]` `ParsedRelatedPart` для аналогов/OEM-связей, которые могут отсутствовать на странице товара.
- `[x]` `GlobalPartFitment` для platform-level применяемости артикула.
- `[x]` применение известных OEM/Cross и fitment данных до внешнего fetch.
- `[x]` базовая platform taxonomy `PartCategory` и seed tenant-категорий автозапчастей при создании tenant-а.
- `[x]` расширенный seed корневых категорий платформы и стартовых tenant-подкатегорий для основных ниш.
- `[x]` локальная БД после merge обновлена миграциями `products.0006` и `products.0007`.

Частично реализовано:

- `[~]` изображения: URL из источника извлекаются и показываются, но автоматическое сохранение в `ProductImage`/S3 pipeline еще не закрыто.
- `[~]` поддержка нескольких источников: source policy и fetcher abstraction есть, но реально подключен только `tachka`.
- `[~]` качество данных: source quality policy добавлен; operator review workflow реализуется для классификации, применяемости и enrichment facts.
- `[~]` массовые действия: batch/cooldown есть, но pause/resume/cancel еще нужно довести в API/UI.
- `[~]` глобальный граф артикулов: модель, обучение, search fallback, source priority и конфликт-правила есть; дальше нужен review workflow.
- `[~]` глобальная применяемость: `GlobalPartFitment` есть, но еще нет нормализованного `VehicleMake/Model/Generation`.

Не реализовано:

- `[ ]` platform-level справочник `VehicleMake/VehicleModel/VehicleGeneration/VehicleModification`.
- `[x]` легкая taxonomy категорий запчастей `PartCategory` с флагом `fitment_required`.
- `[ ]` tenant/catalog capability для отключения автозапчастного enrichment в неавтомобильных нишах.
- `[ ]` мониторинг/алерты качества источников.

---

## Следующий P0 — Source Quality Policy

**Цель:** не дать platform knowledge graph накопить мусор, когда появятся новые источники.

### P0.1 Реестр качества источников

- [x] Ввести единое описание источника: `source_id`, `label`, `priority`, `trust_score`.
- [x] Хранить правила rate limit: `batch_size`, `min_pause_seconds`, `default_pause_seconds`.
- [x] Хранить capability flags: `supports_product_page`, `supports_search`, `supports_fitments`, `supports_images`.
- [x] Хранить transport: `httpx` по умолчанию, будущий `browser`/`cloak` только для сложных источников.
- [x] Не смешивать platform parser source с tenant-owned `DataSourceConnection`.

**Verify:** новый источник можно описать конфигом без изменения `ProductEnrichmentService`.

### P0.2 Confidence policy

- [x] Задать минимальный confidence для автоматического применения relation/fitment к tenant-товару.
- [x] Автоматически ставить `needs_review=True`, если источник низкого trust или parser не уверен.
- [x] Не применять `Unknown` и `needs_review` записи автоматически.
- [x] Повышать confidence только если новый источник равен или надежнее текущего.
- [x] Не понижать уже подтвержденные полезные данные без ручного review.

**Verify:** сомнительная связь сохраняется в global graph, но не попадает в tenant `ProductCrossCode`/`VehicleFitment`.

### P0.3 Conflict policy

- [x] При конфликте источников не удалять старую связь.
- [x] Помечать конфликтующие факты `needs_review`.
- [x] Сохранять provenance: `source_id`, `source_url`, `raw_text`, `last_seen_at`.
- [x] Для fitments не считать аналог доказательством применяемости.
- [x] Для AI отдавать только trusted факты или явно маркировать reviewable данные.

**Verify:** два источника с разными fitments не перетирают друг друга и не ломают описание.

### P0.4 Fetcher abstraction

- [x] Ввести интерфейс fetcher/transport отдельно от parser logic.
- [x] Оставить `HttpxFetcher` как default.
- [x] Добавить возможность source-level выбора browser transport позже.
- [x] Рассматривать `CloakBrowser` как optional future transport для JS/anti-bot источников, не как core dependency.

**Verify:** parser можно тестировать на HTML fixtures без сети и без browser runtime.

---

## Следующий P1 — Operator Review Workflow

**Цель:** дать оператору безопасно подтверждать или отклонять спорные данные,
не перетирая полезные сведения из других источников.

- [x] Добавить `review_status`: `pending`, `approved`, `rejected` для `ProductCatalogClassification`, `VehicleFitment`, `ProductEnrichmentFact`.
- [x] Сохранять audit-поля проверки: `reviewed_at`, `reviewed_by`.
- [x] Добавить tenant-scoped API actions: approve/reject для классификации, применяемости и facts.
- [x] Не применять rejected fitments в `Product.applicability`.
- [x] Дать dashboard-фильтр товаров, где есть данные на проверке.
- [x] Показать approve/reject controls в карточке товара.
- [ ] Расширить очередь проверки отдельным списком/страницей, если объём спорных данных станет большим.

**Verify:** оператор может отклонить спорную применяемость, после чего она остаётся
в истории источника, но не используется в денормализованной применяемости товара.

## Следующий P0 — Vehicle Knowledge Base v1

**Цель:** перестать хранить применяемость только строками и начать нормализовать
марки/модели авто.

### P0.1 VehicleMake / VehicleModel

- [ ] Добавить `VehicleMake`.
- [ ] Добавить `VehicleModel`.
- [ ] Хранить `normalized_name`.
- [ ] Хранить aliases.
- [ ] Сопоставлять `MB`, `Mercedes`, `MERCEDES-BENZ` в одну марку.

**Verify:** разные написания одной марки дают одну запись `VehicleMake`.

### P0.2 Связь с GlobalPartFitment

- [ ] Добавить nullable links `GlobalPartFitment.vehicle_make`.
- [ ] Добавить nullable links `GlobalPartFitment.vehicle_model`.
- [ ] Не удалять raw поля `make/model/generation`.
- [ ] Если нормализация не уверена, оставить raw строки без FK.

**Verify:** raw применяемость сохраняется всегда, normalized FK появляется только при уверенном match.

### P0.3 Frontend cleanup

- [ ] В массовых действиях оставить единый сценарий `Обогатить и сгенерировать`.
- [ ] Убрать старые/дублирующие пункты, которые теперь входят в объединенный pipeline.

**Verify:** пользователь не видит конкурирующие действия, которые запускают разные части одного pipeline.

### P0.4 Нишевая безопасность

- [ ] Зафиксировать, что auto-parts enrichment не должен запускаться автоматически для generic/jewellery/apparel tenant-ов.
- [ ] Спроектировать будущий `catalog_domain`.
- [ ] До появления поля запускать auto-parts enrichment только по явному действию пользователя/API.

**Verify:** обычный tenant с неавтомобильными товарами может использовать импорт, AI и изображения без парсера автозапчастей.

---

## PHASE 0 — Discovery и согласование границ

**Цель:** зафиксировать бизнес-правила до кода.

### 0.1 Решить ownership enrichment-данных

- [ ] Определить, может ли парсер создавать новый `Product`.
- [ ] Зафиксировать, что парсер не обновляет цену, остаток, наличие и склад.
- [ ] Определить, какие поля `Product` можно заполнять только если они пустые.
- [ ] Определить, что важнее при конфликте справочных данных: текущая БД или каталог.
- [ ] Определить, нужна ли ручная модерация до применения данных.
- [ ] Зафиксировать, что парсер является platform-level фичей, а записи enrichment всегда tenant-scoped.
- [ ] Определить, нужен ли общий cache результатов парсинга между tenant-ами.

**Рекомендация эксперта:**
В MVP использовать парсер как enrichment-слой: характеристики, OEM/cross, применяемость, изображения и факты для описания. Коммерческие поля не трогать вообще, даже если источник их показывает.

### 0.2 Собрать HTML fixtures

- [ ] Сохранить несколько HTML fixtures карточек товаров из каталога `tachka`.
- [ ] Сохранить пример прямой карточки товара по `brand + article`.
- [ ] Сохранить пример поиска/перехода из каталога к карточке, если прямой URL не гарантирован.
- [ ] Сохранить пример товара без применяемости.
- [ ] Сохранить пример 404/not found.
- [ ] Сохранить пример с несколькими OEM-производителями.
- [ ] Сохранить пример с несколькими изображениями.
- [ ] Сохранить пример с богатыми характеристиками для описания.

**Verify:** fixtures лежат в тестовой директории, их можно использовать без сети.

### 0.3 Уточнить юридические и технические ограничения источника

- [ ] Проверить robots/rate expectations вручную.
- [ ] Определить допустимую частоту запросов.
- [ ] Согласовать User-Agent.
- [ ] Согласовать поведение при блокировке.

**Verify:** в ТЗ внесены лимиты запросов и правила отказа.

### 0.4 Финализировать MVP

- [ ] Подтвердить список моделей.
- [ ] Подтвердить API.
- [ ] Подтвердить admin actions.
- [ ] Подтвердить критерии `success/need_review/failed`.

**Критерий Phase 0:** команда согласовала, что MVP делает и чего не делает.

---

## PHASE 1 — Модель данных и миграции

**Цель:** добавить только недостающие структуры вокруг текущего `Product`.

### 1.1 Добавить структурные модели

- [ ] `ProductAttribute`
- [ ] `ProductCrossCode`
- [ ] `VehicleFitment`
- [ ] `ProductParseJob`
- [ ] `ProductEnrichmentFact`, если решим хранить факты для описания отдельно от attributes

### 1.2 Индексы и ограничения

- [ ] Все модели результата имеют `tenant` напрямую или tenant доступен через обязательный `product`.
- [ ] Индекс на `ProductCrossCode.normalized_code`.
- [ ] Индекс на `ProductCrossCode.tenant + normalized_code`, если `tenant` хранится напрямую.
- [ ] Индекс на `VehicleFitment.make + model`.
- [ ] Индекс на `VehicleFitment.tenant + make + model`, если `tenant` хранится напрямую.
- [ ] Индекс на `ProductParseJob.tenant + created_at`.
- [ ] Индекс на `ProductParseJob.status + created_at`.
- [ ] Unique constraints против дублей характеристик/кроссов/применяемости.

### 1.3 Backward compatibility

- [ ] Не ломать текущий `Product`.
- [ ] Не удалять `Product.oem_numbers`.
- [ ] Не удалять `Product.cross_numbers`.
- [ ] Не удалять `Product.applicability`.
- [ ] Не менять существующие ProductImage-сценарии.

### 1.4 Миграции

- [ ] Создать миграции.
- [ ] Прогнать migrate локально.
- [ ] Проверить rollback миграций на dev DB.

**Verify:** существующие тесты `products`, `image_search`, `datasources` проходят.

**Критерий Phase 1:** структура данных есть, текущий импорт/товары/изображения не сломаны.

---

## PHASE 2 — Parser Core для tachka

**Цель:** надежно получить structured data из одного источника.

### 2.1 Базовый интерфейс

- [ ] `BasePartParser`.
- [ ] `ParsedPart` DTO/dataclass.
- [ ] `ParsedAttribute`.
- [ ] `ParsedCrossCode`.
- [ ] `ParsedFitment`.
- [ ] `ParserResult`.
- [ ] `ParserError`.

### 2.2 Tachka parser

- [ ] `build_url(brand, article)`.
- [ ] `fetch()`.
- [ ] `parse_product_title()`.
- [ ] `parse_attributes()`.
- [ ] `parse_cross_codes()`.
- [ ] `parse_fitments()`.
- [ ] `parse_image_urls()`.
- [ ] `parse_description_facts()`.

### 2.3 Нормализация

- [ ] `normalize_article()`.
- [ ] `normalize_oem()`.
- [ ] `normalize_brand_for_url()`.
- [ ] `normalize_text_spaces()`.
- [ ] Сохранить ведущие нули в OEM.

### 2.4 Confidence rules

- [ ] Нет title -> fallback title + `need_review`.
- [ ] Нет применяемости -> `need_review`.
- [ ] Нет характеристик и OEM/cross -> `need_review`.
- [ ] Нет recognizable product markers -> `failed`.
- [ ] 404/not found -> `not_found`.

### 2.5 Tests

- [ ] Unit tests на нормализацию.
- [ ] Unit tests на parsing HTML fixtures.
- [ ] Tests на not_found.
- [ ] Tests на broken layout.

**Verify:** parser работает на fixtures без сетевых вызовов.

**Критерий Phase 2:** для набора fixtures из каталога `tachka` получается валидный structured JSON; `BREMBO P50136` может быть одним из примеров, но не единственным сценарием.

---

## PHASE 3 — Сервис сохранения и Celery

**Цель:** связать parser с текущей доменной моделью проекта.

### 3.1 Product enrichment service

- [ ] `create_or_update_product_from_parsed()`.
- [ ] Все операции принимают `tenant` или `Product` с проверенным `tenant`.
- [ ] Правила создания нового `Product`.
- [ ] Правила обновления существующего `Product`.
- [ ] Запрет на обновление `Product.price`.
- [ ] Запрет на обновление `Product.stock_qty`.
- [ ] Запрет на обновление `Product.warehouse`.
- [ ] Правила синхронизации `Product.oem_numbers`.
- [ ] Правила синхронизации `Product.cross_numbers`.
- [ ] Правила синхронизации `Product.applicability`.

### 3.2 Parse job lifecycle

- [ ] Создание `ProductParseJob` с обязательным `tenant`.
- [ ] Переход `pending -> running`.
- [ ] Сохранение `raw_html`.
- [ ] Сохранение `raw_text`.
- [ ] Сохранение `parsed_data`.
- [ ] Переход в финальный статус.
- [ ] Сохранение `error_message`.

### 3.3 Celery

- [ ] `parse_single_part`.
- [ ] `generate_enriched_description` как orchestration task: enrichment -> AI generation.
- [ ] `process_bulk_product_action` как throttled orchestration task.
- [ ] Очередь `part_parsing`.
- [ ] Очередь `part_parsing_bulk`.
- [ ] Retry на network errors.
- [ ] No retry на 404.
- [ ] Timeout.
- [ ] Rate limit.
- [ ] Batch processing для bulk actions.
- [ ] Пауза между batch-ами.
- [ ] Per-tenant concurrency limit.
- [ ] Per-source concurrency/rate limit.
- [ ] Cooldown при 429/timeout spike.
- [ ] Pause/resume/cancel для bulk job.
- [ ] Не списывать AI-кредит до фактического запуска AI-генерации.

### 3.4 Tests

- [ ] Service test: создает новый товар.
- [ ] Service test: обогащает существующий товар.
- [ ] Service test: не нарушает tenant isolation.
- [ ] Service test: одинаковый `brand + article` у разных tenant-ов обогащает разные `Product`.
- [ ] Service test: job одного tenant-а не может обновить товар другого tenant-а.
- [ ] Service test: failed сохраняет ошибку.
- [ ] Celery task test с mock parser.

**Verify:** полный цикл job проходит без ручных действий.

**Критерий Phase 3:** API/Admin может поставить job, Celery сохраняет результат.

---

## PHASE 4 — API и Django Admin

**Цель:** дать пользователю понятное управление и просмотр результата.

### 4.1 API

- [ ] `POST /api/v1/products/parse/`.
- [ ] `GET /api/v1/products/parse-jobs/{id}/`.
- [ ] `GET /api/v1/products/search/?brand=&article=`.
- [ ] `GET /api/v1/products/{id}/fitments/`.
- [ ] `GET /api/v1/products/{id}/cross-codes/`.
- [ ] `POST /api/v1/products/bulk-actions/`.
- [ ] `GET /api/v1/products/bulk-actions/{id}/`.
- [ ] Обновить существующий `POST /api/v1/products/{id}/regenerate/`: запускать enrichment-aware pipeline.
- [ ] Расширить `ProductSerializer` опциональными counts.

### 4.2 Dashboard catalog UX

- [ ] Добавить checkbox у каждой строки каталога товаров.
- [ ] Добавить выбор всех товаров на текущей странице.
- [ ] Добавить выбор всех товаров по текущему фильтру.
- [ ] Добавить счетчик выбранных товаров.
- [ ] Добавить меню быстрых действий.
- [ ] Действие `Обогатить данные`.
- [ ] Действие `Найти изображения`.
- [ ] Действие `Сгенерировать описания`.
- [ ] Действие `Обогатить и сгенерировать описания`.
- [ ] Действие `Проверить применяемость`.
- [ ] Confirmation modal для массовых AI-действий.
- [ ] Progress panel/toast для bulk job.
- [ ] В progress показывать paused/cooling_down и время до следующего batch.
- [ ] Добавить действия pause/resume/cancel для bulk job.
- [ ] Indicators в таблице: фото, описание, enrichment status, применяемость.

### 4.3 Admin

- [ ] Inline характеристик.
- [ ] Inline cross/OEM-кодов.
- [ ] Inline применяемости.
- [ ] Admin для parse jobs.
- [ ] Action `Спарсить заново`.
- [ ] Action `Перезапустить failed jobs`.
- [ ] Search по normalized OEM.
- [ ] Filter по статусу job.

### 4.4 UX админки

- [ ] Raw HTML readonly.
- [ ] Parsed JSON readonly.
- [ ] Error message readonly.
- [ ] Ссылка job -> product.
- [ ] Ссылка product -> последние jobs.

### 4.5 Tests

- [ ] API tenant isolation.
- [ ] API validation errors.
- [ ] API job status.
- [ ] Bulk action не принимает товары другого tenant.
- [ ] Bulk action по фильтру использует tenant-aware queryset.
- [ ] Bulk action не списывает AI-кредиты за skipped товары.
- [ ] Bulk action соблюдает batch size и паузы между batch-ами.
- [ ] Bulk action уходит в cooldown при 429/timeout spike.
- [ ] Admin smoke where practical.

**Verify:** пользователь может запустить парсинг и увидеть результат без shell.

**Критерий Phase 4:** фича пригодна для ручной эксплуатации одним админом.

---

## PHASE 5 — Изображения, описание и качество

**Цель:** закрыть изображения и полезное описание без дублирования уже существующих подсистем.

### 5.1 Изображения

- [ ] Парсер возвращает image URLs.
- [ ] Сервис передает URLs в существующий `ProductImage` pipeline.
- [ ] Ставить статус `needs_review` или `auto_approved` по текущим правилам image_search.
- [ ] Не создавать второй uploader.

### 5.2 Факты для полезного описания

- [ ] Сохранять технические факты, пригодные для описания.
- [ ] Сохранять факты о применяемости.
- [ ] Сохранять предупреждения и неоднозначности.
- [ ] Не включать в описание цену, наличие, склад и сроки поставки.
- [ ] Подготовить черновик описания из фактов, если это согласовано.
- [ ] Передавать enrichment context в существующий `DescriptionAgent`.
- [ ] Обновить prompt агента: применять fitments/OEM/attributes как факты, не придумывать совместимость.
- [ ] Для кнопки `Сгенерировать описание` показывать этапы: обогащение -> генерация -> готово.
- [ ] Если enrichment failed/not_found, разрешить fallback к старой генерации по базовым данным с пониженным confidence.

### 5.3 Качество данных

- [ ] Считать score полноты карточки.
- [ ] Отмечать подозрительные данные.
- [ ] Добавить фильтр `need_review`.
- [ ] Добавить summary в admin.

### 5.4 Monitoring

- [ ] Количество jobs по статусам.
- [ ] Среднее время парсинга.
- [ ] Доля need_review.
- [ ] Доля failed.
- [ ] Алерт при росте failed.

**Verify:** видны причины проблем и динамика качества.

**Критерий Phase 5:** фича не просто работает, а поддерживается без гадания по логам.

---

## PHASE 6 — Масштабирование и AI fallback

**Цель:** расширить сценарии после стабильного MVP.

### 6.1 Массовый парсинг

- [ ] `parse_many_parts`.
- [ ] Batch size.
- [ ] Rate limit per source.
- [ ] Pause/resume.
- [ ] Reparse failed jobs.

### 6.2 Refresh

- [ ] `refresh_part_data`.
- [ ] Не чаще N дней.
- [ ] Обновлять только enrichment-данные.
- [ ] Не перетирать ручные исправления.

### 6.3 AI fallback

- [ ] Использовать AI только для грязных fitment strings.
- [ ] Запрещено AI придумывать новые модели авто.
- [ ] Валидировать JSON-схему ответа.
- [ ] Сохранять confidence.
- [ ] При низкой уверенности ставить `need_review`.

### 6.4 Новые источники

- [ ] `ExistPartParser`.
- [ ] `EmexPartParser`.
- [ ] `AutodocPartParser`.
- [ ] Catalog API parser.
- [ ] Source priority.
- [ ] Merge strategy между источниками.

**Verify:** новый источник добавляется без изменения основного service layer.

**Критерий Phase 6:** модуль готов к росту, но не усложнял MVP заранее.

---

## PHASE 7 — Platform Vehicle Knowledge Base

**Цель:** превратить локально сохраненную применяемость в общую базу знаний проекта, чтобы товары разных tenant-ов могли мгновенно получать известные марки/модели по артикулу или OEM.

### 7.1 Нормализованный справочник автомобилей

- [ ] Добавить `VehicleMake`.
- [ ] Добавить `VehicleModel`.
- [ ] Добавить `VehicleGeneration`.
- [ ] Добавить `VehicleModification`, если данных поколений недостаточно.
- [ ] Добавить aliases для марок и моделей.
- [ ] Нормализовать названия марок: `MERCEDES`, `MB`, `MERCEDES-BENZ`.
- [ ] Нормализовать комбинированные производители: `HYUNDAI / KIA`.
- [ ] Добавить индексы по normalized fields.

**Verify:** одна и та же марка/модель из разных источников сохраняется как одна справочная сущность.

### 7.2 Глобальный индекс применяемости

- [ ] Добавить `GlobalPartFitment` или аналогичный индекс.
- [ ] Хранить ключи `normalized_brand + normalized_article`.
- [ ] Хранить ключи `normalized_oem_code`.
- [ ] Хранить ссылку на `VehicleMake/Model/Generation`.
- [ ] Хранить `source_id`, `source_url`, `confidence`, `needs_review`, `last_seen_at`.
- [ ] Не хранить цены, остатки, склады и tenant-коммерческие данные.
- [ ] Разрешить несколько источников на одну связь.
- [ ] Добавить score доверия источника.

**Verify:** по `BREMBO P50136` или OEM-коду можно получить список применяемости без обращения к внешнему сайту.

### 7.3 Применение знаний к товару tenant-а

- [ ] Перед запуском внешнего parser проверять global index.
- [ ] Если найдено достаточно фактов, создавать/обновлять tenant-scoped `VehicleFitment`.
- [ ] Если данных недостаточно, запускать обычное enrichment.
- [ ] После успешного парсинга обновлять global index.
- [ ] Не смешивать товары разных tenant-ов.
- [ ] Не считать global index пользовательскими данными tenant-а.

**Verify:** второй tenant с тем же артикулом получает применяемость из индекса без повторного парсинга.

### 7.4 UI/API для применяемости

- [ ] В карточке товара показывать применяемость из `VehicleFitment`, примененную из global index или parser.
- [ ] Показывать источник применяемости: parser/cache/global index.
- [ ] В каталоге товаров добавить быстрый признак “есть применяемость”.
- [ ] Добавить фильтр по марке авто.
- [ ] Добавить фильтр по модели авто.
- [ ] Добавить endpoint поиска товаров по авто: `make/model/generation`.

**Verify:** пользователь видит, к каким маркам/моделям подходит запчасть, до генерации описания и без открытия raw parser job.

### 7.5 Контроль качества

- [ ] Все связи с низким confidence помечать `needs_review`.
- [ ] Не давать AI достраивать отсутствующие модели авто.
- [ ] Добавить ручное подтверждение применяемости.
- [ ] При конфликте источников не перетирать подтвержденные данные.
- [ ] Логировать происхождение каждой связи.

**Критерий Phase 7:** применяемость становится переиспользуемым знанием платформы, а не только результатом одного parse job конкретного товара.

---

## Рекомендуемый порядок релизов

### Release 1: Internal MVP

Состав:

- Модели.
- Tachka parser.
- Celery job.
- Admin просмотр.
- Минимальный API.
- Tests на fixtures.

Риск:

- Верстка источника может отличаться от ожиданий.

### Release 2: Operator-ready

Состав:

- Admin actions.
- Need review workflow.
- Изображения через существующий pipeline.
- Больше fixtures.
- Метрики.

Риск:

- Операторам может не хватить объяснения, почему запись попала в `need_review`.

### Release 3: Scalable

Состав:

- Массовый парсинг.
- Refresh.
- Rate limit.
- Reparse failed.

Риск:

- Источник может блокировать при агрессивной частоте.

### Release 4: Smart

Состав:

- AI fallback.
- Новые источники.
- Merge strategy.
- Confidence scoring.

Риск:

- AI может красиво структурировать неверные данные, поэтому нужен строгий guardrail.

---

## Критические решения до старта разработки

1. Парсер создает новые товары или только обогащает существующие?
2. Какие поля `Product` парсер может заполнять, если они пустые?
3. Изображения скачиваем сразу в MVP или оставляем отдельной кнопкой?
4. Генерируем черновик описания в MVP или только сохраняем факты?
5. Какой допустимый rate limit для tachka?
6. Нужно ли хранить raw HTML постоянно или с TTL/cleanup?

---

## Definition of Done для MVP

- [ ] Все новые модели имеют миграции.
- [ ] Все API фильтруются по tenant.
- [ ] Все сетевые вызовы вынесены из request-response в Celery.
- [ ] Парсер покрыт fixture-based tests.
- [ ] Нормализация покрыта unit tests.
- [ ] Job сохраняет raw/parsed/error.
- [ ] Admin позволяет увидеть и перезапустить job.
- [ ] Сохраняются характеристики, OEM/cross, применяемость, изображения/URL и факты для описания.
- [ ] `Product.price`, `Product.stock_qty`, `Product.warehouse` не меняются.
- [ ] Текущие тесты проекта проходят.
- [ ] Нет дублирования `ProductImage`.
- [ ] Нет параллельной сущности `AutoPart`.
