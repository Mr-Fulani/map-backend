# RULEBOOK: Разработка модуля умного парсинга автозапчастей

> Этот документ фиксирует правила разработки фичи, чтобы не расползтись в лишнюю архитектуру и не сломать существующий MAP.

---

## 1. Главный принцип

Модуль парсинга — это обогащение существующих товаров, а не новая параллельная товарная система.

Фича является общей возможностью SaaS-платформы MAP, но результат ее работы всегда относится к конкретному tenant. Один и тот же `brand + article` может существовать у разных tenant-ов, и обогащение не должно смешивать их каталоги.

Правильно:

```text
tenant + Product + ProductAttribute + ProductCrossCode + VehicleFitment + ProductParseJob
```

Неправильно для MVP:

```text
AutoPart + PartImage + PartBrand + отдельный каталог, живущий отдельно от Product
```

---

## 2. Не дублировать существующее

Перед добавлением новой модели/сервиса проверить:

- Есть ли уже похожая сущность в `apps.products`.
- Есть ли уже похожая логика в `apps.image_search`.
- Есть ли уже подходящий Celery/task pattern.
- Есть ли уже API pattern в текущих views.
- Есть ли уже admin pattern через Unfold.

Запрещено:

- Создавать `PartImage`, пока есть `ProductImage`.
- Создавать второй image uploader.
- Создавать второй товарный контур.
- Смешивать каталог `tachka` с `DataSourceConnection`, пока это не tenant-owned API connection.

---

## 3. MVP прежде всего

В MVP реализовать только:

- Один источник: `tachka`.
- Один сценарий: `brand + article -> Product enrichment`.
- Структурные характеристики.
- Структурные OEM/cross-коды.
- Структурную применяемость.
- URL/кандидаты изображений через существующий image pipeline.
- Факты для полезного и достоверного описания.
- Parse job с raw/parsed/error.
- Admin/API для запуска и просмотра.

Не добавлять без отдельного согласования:

- Массовый импорт.
- AI.
- Несколько источников.
- Proxy pool.
- Captcha solving.
- Парсинг цен.
- Парсинг остатков.
- Парсинг наличия на складе.
- Историю остатков.
- Сложную систему брендов.
- TecDoc-совместимость.

---

## 4. Правила работы с `Product`

`Product` остается центральной моделью товара.

Разрешено:

- Заполнять `brand`, `article`, `name`, `category_1c` только при создании товара парсером или если поле пустое и это согласовано.
- Обновлять `oem_numbers`, `cross_numbers`, `applicability` как compatibility fields.
- Добавлять связанные структурные записи.
- Добавлять изображения через существующий `ProductImage` pipeline.
- Добавлять факты для описания.

Запрещено:

- Обновлять `Product.price`.
- Обновлять `Product.stock_qty`.
- Обновлять `Product.warehouse`.
- Обновлять наличие, склад, сроки поставки и условия продажи.
- Использовать внешнюю цену или наличие в описании товара.

Рекомендованное правило:

```text
парсер обогащает характеристики/OEM/fitments/images/description_facts
коммерческие поля не меняет никогда в рамках этой фичи
```

---

## 5. Tenant isolation

Любой запрос к данным должен учитывать tenant. Это не кастомная фича одного tenant-а, а platform feature с tenant-scoped результатами.

Обязательно:

```text
Product.objects.filter(tenant=request.tenant)
ProductParseJob.objects.filter(tenant=request.tenant)
```

Для сервисов:

```text
enrich_product(tenant=tenant, brand=brand, article=article)
save_parsed_result(tenant=tenant, product=product, parsed=result)
```

Перед сохранением обязательно проверить:

```text
product.tenant_id == tenant.id
job.tenant_id == tenant.id
```

Запрещено:

```text
Product.objects.get(pk=pk)
ProductParseJob.objects.get(pk=pk)
Product.objects.filter(article=article).first()
```

Исключения возможны только в Celery-задачах, если tenant уже восстановлен через job/product и дополнительно проверен.

Если нужен общий cache результата парсинга, он может быть platform-level, но применять cached result к БД можно только после tenant-aware поиска товара.

### 5.1 Platform knowledge vs tenant data

Разрешено иметь общую platform-level базу справочных знаний:

```text
VehicleMake
VehicleModel
VehicleGeneration
VehicleModification
GlobalPartFitment
PartVehicleIndex
```

Эти сущности могут переиспользоваться всеми tenant-ами, потому что описывают не каталог клиента, а справочные факты:

```text
марка авто
модель авто
поколение/кузов
применяемость артикула/OEM к авто
source/confidence
```

Запрещено хранить в platform knowledge:

```text
цены tenant-а
остатки tenant-а
склады tenant-а
листинги tenant-а
коммерческие условия tenant-а
ручные приватные заметки tenant-а
```

Правильный поток:

```text
global index нашел применяемость -> создать/обновить VehicleFitment для Product конкретного tenant
```

Неправильно:

```text
показать tenant-у Product другого tenant-а
связать Product tenant-а напрямую с чужим Product
считать global fitment заменой tenant-scoped VehicleFitment
```

### 5.2 Catalog domain guardrail

Автозапчастное обогащение разрешено только для tenant-ов с:

```text
Tenant.catalog_domain == auto_parts
```

Для tenant-ов с `generic`, `jewellery`, `apparel` или `other` запрещено:

```text
создавать ProductParseJob для parser/OEM/fitment enrichment
запускать массовое автозапчастное enrichment-действие
автоматически дергать parser перед AI-генерацией описания
применять PartCategory/VehicleFitment/OEM правила к неавтомобильному каталогу
```

Разрешено для всех catalog domain:

```text
обычная AI-генерация описаний по данным Product
поиск и загрузка изображений через существующий pipeline
импорт, редактирование и публикация товаров
```

Если нужен новый неавтомобильный домен, нельзя переиспользовать auto-parts parser.
Нужно добавить отдельную domain-specific enrichment-логику и явно подключить ее к
tenant capability.

---

## 6. Сетевые вызовы

Запрещено парсить внешний сайт прямо в HTTP request-response.

Правильно:

```text
API/Admin создает ProductParseJob
API/Admin ставит Celery task
Celery делает fetch/parse/save
API/Admin показывает статус
```

Для существующей кнопки `Сгенерировать описание`:

```text
Dashboard/API получает запрос на генерацию
orchestration task проверяет свежесть enrichment
если нужно — запускает parser/enrichment
после enrichment запускает DescriptionAgent
AI сохраняет title_ai/description_ai
```

Запрещено:

```text
кнопка генерации описания напрямую дергает внешний сайт
AI-кредит списывается до фактического запуска AI
AI пишет применяемость, которой нет в enrichment/raw данных
```

Для HTTP:

- timeout обязателен;
- retry только для временных сетевых ошибок;
- не retry для 404;
- ограничивать размер HTML;
- сохранять финальный URL после redirect;
- использовать понятный User-Agent;
- не делать агрессивный scraping.

### 6.1 Source policy

Все настройки источника должны жить в едином policy-слое, а не размазываться по
parser/service/view.

Правильно:

```text
apps.products.source_policy.get_part_source_policy(source_id)
```

Policy должен описывать:

```text
priority
trust_score
batch_size
pause limits
capabilities
transport
auto_apply_min_confidence
```

Автоматически применять к tenant-товару можно только данные, которые прошли policy:

```text
needs_review = false
confidence >= auto_apply_min_confidence
relation_type != Unknown
```

Запрещено:

```text
добавлять новый источник без policy
вшивать source priority прямо в parser
подключать browser/anti-bot runtime как обязательную зависимость core parser
считать аналог доказательством применяемости
```

Для массовых действий:

```text
никогда не запускать тысячи parse jobs одновременно
обрабатывать batch-ами
делать паузы между batch-ами
ограничивать параллелизм на tenant и source
автоматически уходить в cooldown при 429/timeout spike
```

Минимальные controls:

```text
pause bulk job
resume bulk job
cancel bulk job
cooling_down status
next_batch_at timestamp
```

Запрещено:

```text
bulk action -> for product in products: parse_single_part.delay(...) без лимитов
игнорировать 429/rate limit
ретраить массово без паузы
```

---

## 7. Raw данные

Каждый parse job должен сохранять:

- `raw_html`;
- `raw_text`;
- `parsed_data`;
- `source_url`;
- `error_message`;
- `status`;
- timestamps.

Правило:

```text
если данные нельзя объяснить из raw_html/raw_text, фича не отлаживаема
```

Если raw HTML станет слишком большим:

- сначала добавить лимит размера;
- потом обсудить TTL/cleanup;
- не удалять raw без решения по observability.

---

## 8. Нормализация

Нормализация артикула и OEM должна быть единой функцией, покрытой тестами.

Минимальное правило:

```text
upper()
убрать пробелы, дефисы, точки, слеши и прочие разделители
оставить буквы и цифры
не удалять ведущие нули
```

Примеры:

```text
P 50 136 -> P50136
P-50-136 -> P50136
p 50 136 -> P50136
A 000 420 60 00 -> A0004206000
0004206000 -> 0004206000
```

Нельзя:

- выкидывать ведущие нули;
- приводить OEM к int;
- делать разные нормализаторы в разных модулях.

---

## 9. Parser design

Парсер должен возвращать DTO/structured result, а не сразу писать в базу.

Правильно:

```text
fetch HTML
parse HTML -> ParsedPart
validate ParsedPart
save ParsedPart через service
```

Неправильно:

```text
parser внутри себя создает Product, ProductAttribute, VehicleFitment
```

Зачем:

- parser проще тестировать на fixtures;
- save logic не зависит от HTML;
- новый источник можно добавить без переписывания сохранения.

---

## 10. AI guardrails

AI не является источником истины.

AI-описание должно запускаться после enrichment, если enrichment отсутствует или устарел. Старый сценарий генерации только по `Product.name/description_1c` допустим как fallback, но должен иметь более низкий confidence.

AI запрещено:

- придумывать применяемость;
- добавлять автомобили, которых нет в raw данных;
- исправлять OEM без исходного основания;
- возвращать данные без confidence/need_review.

AI разрешено:

- структурировать грязную строку;
- нормализовать написание;
- предложить категорию;
- пометить подозрение.
- писать заголовок и описание на основе уже сохраненных enrichment-фактов.

Перед вызовом AI нужно собрать context:

```text
Product base fields
ProductAttribute[]
ProductCrossCode[]
VehicleFitment[]
ProductEnrichmentFact[]
последний ProductParseJob.status/source_url
```

В prompt обязательно указать:

```text
не придумывать совместимость
если применяемость не подтверждена — писать осторожно
не использовать цену/наличие/склад из внешнего источника
```

Любой AI-результат должен:

- ссылаться на raw input;
- проходить JSON/schema validation;
- сохраняться с confidence;
- уходить в `need_review` при сомнениях.

---

## 11. Quality statuses

Использовать ограниченный набор статусов:

```text
pending
running
success
failed
not_found
need_review
```

Не добавлять новые статусы без причины.

Правила:

- `success`: обязательные данные есть, критичных замечаний нет.
- `need_review`: данные частично есть, но есть риск качества.
- `not_found`: товар не найден источником.
- `failed`: техническая ошибка или сломанная структура.

---

## 12. Работа с изображениями

Использовать существующий `ProductImage`.

Нельзя:

- создавать `PartImage`;
- сохранять файлы в обход текущего storage без причины;
- писать второй pipeline дедупликации.

Если parser нашел image URLs:

```text
передать URL в существующий image pipeline
или сохранить как parsed_data до этапа изображений
```

---

## 13. Тесты обязательны

Минимальный набор перед merge:

- нормализация article/OEM;
- parsing HTML fixture;
- saving parsed result;
- status `success`;
- status `need_review`;
- status `not_found`;
- status `failed`;
- tenant isolation;
- API validation.

Не использовать реальные внешние сайты в CI.

CI должен работать на fixtures/mocks.

---

## 14. Ошибки и деградация

Не скрывать ошибки.

При исключении:

- записать `ProductParseJob.status = failed`;
- записать `error_message`;
- сохранить raw HTML, если он был получен;
- не оставлять job в `running`.

При частичном результате:

- сохранить то, что валидно;
- поставить `need_review`;
- объяснить причину в parsed metadata/error notes.

---

## 15. Админка

Admin должен помогать оператору понять, что произошло.

Обязательно показать:

- входной brand/article;
- source;
- status;
- source_url;
- product link;
- error;
- parsed JSON;
- timestamps.

Для связанных данных:

- attributes inline;
- cross codes inline;
- fitments inline;
- latest jobs readonly/link.

---

## 16. API

API должно быть асинхронным:

```text
POST parse -> job_id/task_id
GET job -> status/result
```

Нельзя:

- ждать завершения внешнего парсинга в POST;
- отдавать данные другого tenant;
- возвращать traceback пользователю.

---

## 17. Миграции

Миграции должны быть обратимо безопасными для текущих данных.

Правила:

- Новые поля nullable/default там, где есть существующие строки.
- Не менять существующие constraints без отдельного решения.
- Не удалять старые поля ради “чистоты”.
- Не делать data migration на весь каталог без оценки объема.

---

## 18. Производительность

MVP не оптимизировать преждевременно, но соблюдать базовые вещи:

- bulk_create для связанных строк;
- индексы на normalized_code/status/tenant;
- prefetch для API detail;
- лимитировать количество fitments/crosses на один parse;
- не хранить бесконечные HTML.

---

## 19. Review checklist

Перед merge проверить:

- Нет ли новой параллельной товарной модели.
- Нет ли второго image pipeline.
- Все querysets tenant-aware.
- Сетевые вызовы только в Celery/service, не в API view.
- Parser тестируется без базы.
- Save service тестируется без сети.
- Raw/parsed/error сохраняются.
- Старые тесты проекта проходят.
- Изменения минимальны для задачи.

---

## 20. Git workflow

Все работы по модулю обогащения вести через отдельные feature-ветки.

### 20.1 Создание ветки

Правило именования:

```text
feature/part-enrichment-<short-task>
```

Примеры:

```text
feature/part-enrichment-models
feature/part-enrichment-tachka-parser
feature/part-enrichment-bulk-actions
feature/part-enrichment-ai-flow
```

Перед созданием ветки:

```text
git status
git fetch origin
git switch develop
git pull --ff-only
git switch -c feature/part-enrichment-<short-task>
```

Если в рабочем дереве есть чужие незакоммиченные изменения, не трогать их и не делать `git reset --hard`.

### 20.2 Перед коммитом

Перед коммитом обязательно:

```text
git status
pytest для затронутого backend-кода
npm test/lint/build для затронутого frontend-кода, если применимо
makemigrations --check, если менялись модели
```

Минимальный backend-check для этой фичи:

```text
docker compose exec backend poetry run pytest apps/products apps/ai_agent apps/image_search
```

Если тесты не запускались или часть тестов упала, это нужно явно написать в PR.

### 20.3 Коммит

Коммитить только связанные с задачей файлы.

Не добавлять в коммит:

```text
локальные env-файлы
временные HTML fixtures с приватными данными
логи
node_modules
__pycache__
чужие изменения
```

Формат сообщения:

```text
feat(products): add enrichment parse jobs
fix(products): keep enrichment tenant-scoped
test(products): cover article normalization
docs(enrichment): clarify bulk throttling
```

### 20.4 Push и PR

После успешных тестов:

```text
git push -u origin feature/part-enrichment-<short-task>
```

В PR указать:

```text
что изменилось
какие модели/миграции добавлены
какие API endpoints добавлены
как проверен tenant isolation
какие тесты запускались
известные ограничения
```

Для задач с миграциями PR должен явно описывать порядок deploy:

```text
deploy code
run migrations
restart workers
verify queues
```

### 20.5 Merge

Мержить только после:

```text
review approved
CI green
миграции проверены
нет конфликтов
нет незадокументированных breaking changes
```

Предпочтительно:

```text
squash merge для небольших задач
merge commit для крупных фаз с несколькими осмысленными коммитами
```

Не мержить, если:

```text
тесты красные
tenant isolation не покрыт
bulk throttling не работает
есть риск перезаписи price/stock/warehouse
```

### 20.6 После merge

После успешного merge:

```text
git switch develop
git pull --ff-only
git branch -d feature/part-enrichment-<short-task>
git push origin --delete feature/part-enrichment-<short-task>
```

Если ветка не удаляется локально через `-d`, сначала проверить, что она действительно смержена. Не использовать `-D` без осознанной причины.

---

## 21. Stop conditions

Остановиться и согласовать, если:

- источник блокирует запросы;
- структура сайта не позволяет стабильно парсить;
- данные источника конфликтуют с 1С;
- появляется требование использовать цену, остаток, наличие или склад из внешнего источника;
- нужно добавить AI;
- нужно добавить второй источник;
- raw HTML содержит неожиданные персональные/закрытые данные;
- реализация требует крупного рефакторинга существующих товаров.
