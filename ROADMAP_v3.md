# ROADMAP v3: Поиск изображений для автозапчастей (Исправленный)

> [!WARNING]
> **Статус: исторический артефакт, не runbook.** Чеклисты, версии зависимостей и
> инфраструктурные команды ниже описывают исходный план и намеренно не обновляются
> как operational truth. Для текущего запуска и deploy используйте
> [README](README.md), [DEV](DEV.md),
> [production deployment](docs/DEPLOYMENT.md),
> [security runbook](docs/PRODUCTION_SECURITY.md) и
> [backup/restore runbook](docs/BACKUP_RESTORE.md). Актуальные зависимости заданы
> в `requirements/*.in`, сгенерированных hash-lock `requirements/*.txt`,
> `frontend/package.json`/`package-lock.json` и Dockerfile-ах.

> **Адаптировано:** под существующую архитектуру MAP  
> **Хранилище:** Yandex Cloud S3 (текущее)  
> **Источники:** Free-only (pluggable на будущее)  
> **Оценка:** 9 недель, 1 backend-разработчик  
> **Изменения vs v2:** YC S3 вместо R2, расширение существующей ProductImage, отдельный Celery worker, tenant isolation, убран trust_level

---

## Обзор фаз

```
PHASE 0      PHASE 1      PHASE 2      PHASE 3      PHASE 4      PHASE 5
────────     ────────     ────────     ────────     ────────     ────────
Infra &      Pluggable    Storage &    Pipeline &   Dashboard    Monitor &
Setup        Sources      SEO Files    Quality      Integration  Optimize
  1 нед        2 нед        2 нед        2 нед        1 нед        1 нед
```

---

## PHASE 0 — Инфраструктура и настройка (1 неделя)

**Цель:** Структура модулей, расширение моделей, Celery-очереди, зависимости.

### 0.1 Проверка YC S3

- [ ] Убедиться что `YC_S3_*` env-переменные настроены
- [ ] Написать management command `python manage.py test_s3_connection` — загрузить тестовый файл, прочитать, удалить
- [ ] Проверить CDN-доступ через `YC_CDN_DOMAIN`

### 0.2 Новое приложение `apps/image_search/`

```
apps/image_search/
├── __init__.py
├── apps.py
├── models.py              # ImageSearchLog, ImageSearchCache
├── admin.py
├── serializers.py
├── views.py
├── urls.py
├── tasks.py               # Celery tasks
├── services/
│   ├── __init__.py
│   ├── pipeline.py        # ImageSearchPipeline
│   ├── quality.py         # QualityScorer
│   ├── query_builder.py   # build_queries()
│   └── storage_utils.py   # SEO-имена, resize, slugify_ru()
└── sources/
    ├── __init__.py
    ├── base.py            # BaseImageSource, ImageCandidate
    ├── registry.py        # register(), get_active_sources()
    ├── autodoc.py         # Tier 1
    ├── exist.py           # Tier 2
    ├── emex.py            # Tier 3
    └── duckduckgo.py      # Tier 4
```

- [ ] Создать структуру, пустые файлы с docstrings
- [ ] Добавить `apps.image_search` в `INSTALLED_APPS` (LOCAL_APPS)

### 0.3 Расширить `ProductImage` в `apps/products/models.py`

Добавить поля (все nullable/defaults для обратной совместимости):
- [ ] `status` (default='imported'), `source_id`, `quality_score`, `search_confidence`
- [ ] `phash` (db_index=True), `s3_key_preview`, `original_url`
- [ ] `resolution_w`, `resolution_h`, `file_size_kb`, `tier`
- [ ] `is_primary`, `seo_filename`, `reviewed_at`, `reviewed_by`

Добавить `image_status` в `Product`:
- [ ] `image_status` — CharField (blank=True, default='')

- [ ] `makemigrations products && migrate`

### 0.4 Зависимости → `requirements/base.txt`

```
httpx==0.27.0
selectolax==0.3.21
imagehash==4.3.1
transliterate==1.10.2
```

### 0.5 Celery: очереди + отдельный worker

- [ ] Добавить `image_search`, `image_search_bulk` в `CELERY_TASK_QUEUES`
- [ ] Добавить `celery_worker_images` в `docker-compose.yml` (concurrency=2, только image_search очереди)
- [ ] Добавить beat-задачи в `CELERY_BEAT_SCHEDULE`

### 0.6 Settings

- [ ] Создать `config/settings/image_search.py`
- [ ] Импортировать в `base.py`

**Критерий Phase 0:** Миграции применены. `docker compose up` поднимает image worker. `test_s3_connection` проходит.

---

## PHASE 1 — Pluggable Sources (2 недели)

### Неделя 1 — Ядро

- [ ] `ImageCandidate` dataclass + `BaseImageSource` ABC
- [ ] `register()` декоратор + `get_active_sources()` в registry
- [ ] `build_queries(product)` — использует `product.brand`, `product.article`, `product.name`
- [ ] `_is_unreliable_article(article, brand)`
- [ ] `AutodocSource` (Tier 1) — HTML scraping через selectolax + httpx

### Неделя 2 — Остальные источники

- [ ] `ExistSource` (Tier 2), `EmexSource` (Tier 3), `DuckDuckGoSource` (Tier 4)
- [ ] Интеграционный тест на 20 артикулах

**Критерий:** 4 источника возвращают `list[ImageCandidate]` без исключений.

---

## PHASE 2 — Storage & SEO Files (2 недели)

### Неделя 3 — Имена и пути

- [ ] `slugify_ru()`, `build_seo_filename()`, `build_s3_path()`
- [ ] 3 версии: original (1600px), preview (600px), thumb (150px)
- [ ] Переиспользовать `_resize` / `_to_jpeg_bytes` из `apps/products/storage.py`
- [ ] Модели `ImageSearchLog` (с FK tenant!), `ImageSearchCache` в `apps/image_search/models.py`

### Неделя 4 — Cleanup и дедупликация

- [ ] `post_delete` на `ProductImage` → удалить файлы из S3
- [ ] Phash-дедупликация (расстояние < 10)
- [ ] Кеш поиска с TTL 7 дней

**Критерий:** 5 товаров → SEO-файлы в S3 → удаление → cleanup.

---

## PHASE 3 — Pipeline & Quality (2 недели)

### Неделя 5 — Pipeline + QualityScorer

- [ ] QualityScorer с 5 критериями
- [ ] ImageSearchPipeline: каскад, ранняя остановка, phash+SHA256 дедупликация
- [ ] Tenant isolation во всех querysets
- [ ] SourceRateLimiter per-source в Redis

### Неделя 6 — Celery tasks

- [ ] `search_images_for_product` (queue=image_search)
- [ ] `search_images_bulk` (queue=image_search_bulk, chunks по 50)
- [ ] `refresh_stale_images` (beat, ночь)
- [ ] Task paths: `apps.image_search.tasks.*`

**Критерий:** batch 50 товаров, coverage ≥ 60%, ≤ 8 сек/товар.

---

## PHASE 4 — Dashboard Integration (1 неделя)

- [ ] DRF API с tenant-фильтрацией
- [ ] Упрощённый `display_mode` (без trust_level): auto/review/suspicious/missing
- [ ] Async polling (task_id → AsyncResult)
- [ ] Django Admin: ProductImageInline, действие «Поиск фото», фильтры
- [ ] UNFOLD sidebar: пункт «Изображения»

**Критерий:** Полный цикл: поиск → превью → одобрение → публикация.

---

## PHASE 5 — Monitoring & Optimize (1 неделя)

- [ ] Management commands: `image_search_stats`, `image_search_reset`, `image_search_bulk_import`
- [ ] Beat-задача `check_source_health()` → автоотключение + алерт через `NotificationService`
- [ ] Алерты: coverage < 70%, queue > 500
- [ ] Документация + `.env.example` + Swagger

---

## Milestones

| # | Milestone | Срок | Критерий |
|---|---|---|---|
| M0 | Инфра готова | Конец нед. 1 | Миграция, worker стартует |
| M1 | 4 источника | Конец нед. 3 | Каждый возвращает кандидатов |
| M2 | SEO-файлы в S3 | Конец нед. 5 | 3 версии, cleanup |
| M3 | Pipeline + Celery | Конец нед. 7 | coverage ≥ 60% |
| M4 | Dashboard | Конец нед. 8 | Модератор управляет фото |
| M5 | Мониторинг | Конец нед. 9 | Алерты, stats |

## MVP — минимум за 4 недели

```
Phase 0 → Phase 1 (только autodoc + duckduckgo) → Phase 2 → Phase 4 (Django Admin)
```
