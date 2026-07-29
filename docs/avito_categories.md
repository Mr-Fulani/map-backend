# Дерево категорий Avito в каталоге

Каталог тенанта (`TenantCatalogCategory`) для авто-домена строится из **полного
дерева категорий Avito** — категории → подкатегории → виды запчастей (самый
глубокий уровень, напр. `Двигатель → Патрубки вентиляции`).

Дерево **вшито в код** в `apps/marketplaces/data/avito_tree_<domain>.json` и
редактируется/дополняется/удаляется тенантом через дашборд (Настройки → Категории).

## Как устроено Avito API (важно)

- `GET /autoload/v1/user-docs/tree` — дерево категорий. Большинство доменов отдаёт
  глубоко вложенным, **но ветку «Запчасти и аксессуары» отдаёт усечённой** (только
  name/slug, без детей). Для неё используем готовый `data/avito_field_specs.json`
  (полные пути листьев).
- `GET /autoload/v1/user-docs/node/{slug}/fields` — поля листа. Виды запчастей —
  это значения полей `SparePartType` / `EngineSparePartType` / `BodySparePartType` /
  `TransmissionSparePartType`. Часть значений приходит **инлайн**, часть — по ссылке
  `values_link_json` (требует авторизации). Команда тянет оба варианта.

## Как добавить новый домен (напр. Одежда)

1. **Узнать корневую категорию Avito** для домена (верхний уровень дерева):
   ```bash
   # см. имена верхнего уровня (Личные вещи, Электроника, ...)
   python manage.py shell -c "from apps.marketplaces.models import MarketplaceAccount; \
     from apps.marketplaces.adapters.avito.adapter import AvitoAdapter; \
     acc=MarketplaceAccount.objects.filter(marketplace='avito', is_active=True).first(); \
     print([n['name'] for n in AvitoAdapter(acc).get_category_tree()])"
   ```
   Одежда лежит в `Личные вещи → Одежда, обувь, аксессуары`.

2. **Сгенерировать дерево** (вшивается в код как JSON):
   ```bash
   # один домен:
   python manage.py sync_avito_full_tree --root "Одежда, обувь, аксессуары" --domain apparel
   # ВСЕ домены верхнего уровня Avito за раз (файлы по avito-slug):
   python manage.py sync_avito_full_tree --all
   ```
   Создаст `apps/marketplaces/data/avito_tree_<slug>.json`. Закоммитить.
   Виды запчастей (`*SparePartType`, инлайн и по `values_link_json`) тянутся
   автоматически; для усечённого Avito-корня «Запчасти и аксессуары» — фолбэк на
   `avito_field_specs.json` (полное дерево запчастей — отдельный файл
   `avito_tree_auto_parts.json`, он канонический для запчастей).

3. **Импортировать в каталог тенантов** (идемпотентно):
   ```bash
   python manage.py import_avito_tree --domain apparel            # все тенанты
   python manage.py import_avito_tree --domain apparel --tenant X # один тенант
   ```

4. Для домена `auto_parts` это дерево является приоритетным: оно автоматически
   импортируется при создании/включении домена и командой
   `seed_tenant_categories` во время деплоя. Импорт не удаляет пользовательские
   категории и прежние назначения товаров. Старый шаблон
   `platform_auto_parts_seed` остаётся внутренним словарём автоопределения, но
   после появления дерева Avito не показывается в интерфейсе назначения.
   Переносить уже назначенные товары и удалять старый шаблон можно только
   отдельно, после проверки через `dedupe_auto_parts_categories --dry-run`.

## Файлы

- `apps/marketplaces/management/commands/sync_avito_full_tree.py` — сбор дерева из Avito → JSON.
- `apps/marketplaces/management/commands/import_avito_tree.py` — импорт JSON → каталог тенанта.
- `apps/marketplaces/avito_tree_import.py` — `AvitoTreeImporter`.
- `apps/marketplaces/data/avito_tree_<domain>.json` — вшитые деревья (источник истины).

## Привязка к публикации (маппинг)

Категория каталога → Avito определяется при выгрузке фида (см. `feed_builder._get_category_mapping`,
`CategoryMapping`). Виды запчастей соответствуют `SparePartType`/`*SparePartType` —
Avito также доопределяет их по названию объявления.
