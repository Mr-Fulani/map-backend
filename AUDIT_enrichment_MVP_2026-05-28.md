# AUDIT: Enrichment MVP

> [!WARNING]
> **Исторический снимок аудита на 2026-05-28, не текущий статус и не runbook.**
> Часть перечисленных gaps уже закрыта. Актуальные команды и production-контракт:
> [README](README.md), [DEV](DEV.md),
> [deployment](docs/DEPLOYMENT.md) и
> [release checklist](docs/RELEASE_CHECKLIST.md).

> Date: 2026-05-28  
> Scope: product enrichment, parser, Celery, API, dashboard, AI description context  
> Result: MVP core is mostly implemented; release-readiness requires closing several operational gaps.

---

## 1. Current Stage

Project status against `ROADMAP_умный_парсинг_автозапчастей_v1.md`:

```text
Current stage: between PHASE 4 and PHASE 5
```

Implemented:

- Tenant-scoped enrichment models: `ProductAttribute`, `ProductCrossCode`, `VehicleFitment`, `ProductEnrichmentFact`, `ProductParseJob`.
- Bulk action model: `ProductBulkActionJob`.
- First parser source: `TachkaPartParser`.
- Parser result save flow: attributes, OEM/Cross, fitments, description facts, raw HTML/text/JSON.
- Merge-first save strategy: repeated enrichment does not wipe existing useful data.
- Celery tasks: single parse, parse-then-generate, throttled bulk job.
- API: parse, parse job status, bulk action, bulk status, fitments, cross-codes.
- Dashboard: enrichment status badges, product detail enrichment block, one button for enrichment + generation.
- AI: description agent receives enrichment context and rejects vague fitment phrases.
- Bulk action batching with cooldown/pause between batches.

Partially implemented:

- Image URLs are extracted and shown, but not automatically saved into `ProductImage` storage pipeline.
- Source architecture exists, but only `tachka` is implemented.
- `need_review`/`confidence` exists, but there is no full operator review workflow.
- Bulk jobs have statuses for pause/cancel, but no API/UI controls yet.

Not implemented:

- Platform-level vehicle knowledge base.
- Global part/OEM -> vehicle fitment index shared across tenants.
- Instant fitment application from global index before parser call.
- Multi-source priority and conflict resolution.
- Source monitoring/alerts.

---

## 2. Verification Run

Passed:

```text
docker compose exec django pytest \
  apps/products/tests/test_enrichment_models.py \
  apps/products/tests/test_part_parsers.py \
  apps/products/tests/test_enrichment_api.py \
  apps/products/tests/test_bulk_actions.py \
  apps/ai_agent/tests/test_agent.py -v

39 passed
```

Passed:

```text
docker compose exec django python manage.py check
System check identified no issues
```

Passed:

```text
docker compose exec django python manage.py showmigrations products
0001..0005 applied
```

Passed:

```text
npm run lint
No ESLint warnings or errors
```

---

## 3. Release Blockers

### P0. Existing `/regenerate/` API is not enrichment-aware — CLOSED

Status:

```text
Closed on 2026-05-28.
```

What changed:

- `POST /api/v1/products/{id}/regenerate/` now checks AI quota without spending credits.
- The endpoint creates tenant-scoped `ProductParseJob`.
- The endpoint enqueues `parse_single_part_then_generate_description`.
- API and dashboard now use the same enrichment -> AI generation behavior.
- Added tests for successful enqueue and cross-tenant rejection.

### P0. Product images from parser are not saved to `ProductImage` — CLOSED

Status:

```text
Closed on 2026-05-28.
```

What changed:

- Parser tasks now enqueue `download_enrichment_images` after successful enrichment.
- The image task runs in the existing `image_search` queue.
- The task uses the existing `PhotoUploadPipeline`; no second uploader was introduced.
- Enrichment images are saved as `ProductImage` with `source_id` and `needs_review` status.
- Added tests for task enqueue and image metadata.

### P0. Bulk progress counters are misleading — CLOSED

Status:

```text
Closed on 2026-05-28.
```

What changed:

- Dashboard no longer shows `success_count`/`failed_count` as if bulk job tracks final parser outcomes.
- Progress block now says `Массовая постановка задач`.
- UI shows `processed_count`, `queued_count`, `skipped_count`, and cooldown/next batch time.
- Accurate final child parse aggregation remains a future enhancement, not MVP blocker.

---

## 4. High Priority Gaps

### P1. No pause/resume/cancel API for bulk jobs

Model statuses exist: `paused`, `cancelled`, `cooling_down`. But API currently only supports create and status read.

Required:

- `POST /api/v1/products/bulk-actions/{id}/pause/`
- `POST /api/v1/products/bulk-actions/{id}/resume/`
- `POST /api/v1/products/bulk-actions/{id}/cancel/`
- Dashboard buttons for active bulk job.
- Tests for tenant isolation and state transitions.

### P1. Network retry semantics are weaker than roadmap

Celery tasks have retry wrappers, but `run_parse_job()` catches generic parser/fetch exceptions and marks the job `failed`, so temporary network errors may not retry as intended.

Required:

- Introduce parser exception types: `TemporarySourceError`, `SourceBlockedError`, `ParserLayoutError`.
- Retry only temporary network/5xx/timeouts.
- No retry for 404/not found.
- Add tests for timeout/5xx/404 behavior.

### P1. Raw HTML has no size/TTL policy

`ProductParseJob.raw_html` is saved directly. This is useful for debugging, but can grow DB size quickly.

Required:

- Add max raw HTML size before save.
- Add cleanup policy or management command.
- Keep `raw_text` and `parsed_data` for longer than full HTML if needed.

### P1. Operator review workflow is partially implemented

We store `needs_review`, `need_review`, confidence, provenance and review status.
The product detail dashboard can approve/reject suspicious classifications,
fitments and enrichment facts.

Required:

- [x] Add dashboard filter for products with `needs_review`.
- [x] Add actions: approve/reject fitment, classification and enrichment fact.
- [x] Prevent rejected fitments from being used in denormalized applicability.
- [ ] Add a dedicated review queue page if the inline product workflow becomes too slow for operators.

---

## 5. Medium Priority Gaps

### P2. Product list prefetches all parse jobs for page products

Product list prefetches `parse_jobs` ordered by date and then reads the first job in serializer. For many historical jobs this can become heavy.

Required:

- Replace with latest-job annotation/subquery.
- Or prefetch only latest job via a separate relation/pattern.

### P2. No product search by OEM/model yet

API can read a product's fitments/cross-codes, but cannot search catalog by:

```text
OEM code
vehicle make
vehicle model
generation
```

Required:

- Add search/filter endpoints after global vehicle index design.
- Until then, add tenant-scoped filters using `ProductCrossCode` and `VehicleFitment`.

### P2. Source abstraction is minimal

`get_part_parser()` and source config exist, but there is no explicit `BasePartParser` interface.

Required before second source:

- Define parser contract.
- Define source capabilities.
- Define merge priority per source.
- Add fixture test pattern for each source.

### P2. Dashboard bulk UX is basic

Current UI shows progress and manual refresh, but not `next_batch_at`, estimated wait, or cooldown explanation.

Required:

- Show `cooling_down` as user-friendly text.
- Show next batch time.
- Add pause/cancel buttons once API exists.

---

## 6. Next Architecture Step

The next major feature should be:

```text
Platform Vehicle Knowledge Base
```

Why:

```text
Current VehicleFitment is tenant/product-local.
We need reusable platform knowledge:
brand/article/OEM -> make/model/generation/modification.
```

Target behavior:

1. Tenant opens/imports a product.
2. System normalizes brand, article, OEM/Cross.
3. System checks global index first.
4. If hit: applies known fitments to tenant product instantly.
5. If miss: runs parser.
6. Parser result updates both tenant product and global index.

This should be implemented after MVP release blockers, not before them.

---

## 7. Recommended Execution Order

### Step 1: Stabilize MVP release

- [ ] Make `/regenerate/` enrichment-aware.
- [ ] Save parser images into existing `ProductImage` pipeline.
- [ ] Fix bulk counters or UI wording.
- [ ] Add tests for the three items above.

### Step 2: Operator readiness

- [ ] Add bulk pause/resume/cancel API and UI.
- [ ] Add `need_review` review filters/actions.
- [ ] Show better bulk cooldown state.
- [ ] Add raw HTML size policy.

### Step 3: Source robustness

- [ ] Add temporary/permanent parser error classes.
- [ ] Implement retry semantics for timeout/5xx.
- [ ] Add more Tachka fixtures.
- [ ] Add source health metrics.

### Step 4: Vehicle knowledge base

- [ ] Add `VehicleMake`, `VehicleModel`, `VehicleGeneration`.
- [ ] Add `GlobalPartFitment`.
- [ ] Add lookup-before-parse flow.
- [ ] Add tenant application from global index.
- [ ] Add search/filter by vehicle.

---

## 8. Release Recommendation

Do not start PHASE 7 implementation before closing the P0 items.

Recommended next coding task:

```text
Make the existing regenerate endpoint use the same enrichment -> AI generation pipeline as the dashboard button.
```

Reason:

```text
It removes a behavioral split: UI button and API endpoint should not produce different quality descriptions.
```
