# Avito Autoload — поля фида

Публикация на Avito идёт **только через Autoload-фид** (XML `formatVersion="3"`):
генерируем XML → кладём на S3 → `POST /autoload/v1/upload` → Avito асинхронно
создаёт объявления. Сборка фида — `apps/marketplaces/adapters/avito/feed_builder.py`
(`build_feed`). Результат опрашивается `poll_feed_results_task`.

Набор обязательных полей **зависит от категории Avito**. Ниже — для текущей
основной категории **«Запчасти и аксессуары»** (подтверждено реальными
ответами Avito Autoload).

## Обязательные поля (категория «Запчасти и аксессуары»)

| Тег фида | Откуда берём в коде | Fallback / правило |
|---|---|---|
| `Id` | `listing.publish_idempotency_key` (UUID) | — ключ сопоставления с avito_id |
| `Title` | `listing.title` | → `product.name` |
| `Description` | `listing.description_ai` | → `product.description_1c` |
| `Price` | `listing.price_on_listing` | целое |
| `Category` | `CategoryMapping.category_target` | → дефолт «Запчасти и аксессуары» |
| `AdType` (Вид объявления) | `listing.ad_type` | → «Товар приобретен на продажу». «Продаю своё» Avito не принимает для запчастей |
| `GoodsType` (Вид товара) | `CategoryMapping.attributes_map['GoodsType']` | → дефолт «Запчасти» |
| `ProductType` (Тип товара) | `CategoryMapping.attributes_map['ProductType']` | дефолта нет; для запчастей = класс техники, напр. «Для автомобилей» |
| `Brand` (Производитель) | `product.brand` | → название организации тенанта (`tenant.name`) |
| `OEM` (Номер детали) | `product.oem_numbers` | → `product.article` → сгенерированное `NA…`; при отсутствии реального OEM тенант предупреждается в Логах |
| `Address` **или** `SellerAddressID` | цепочка размещения (см. ниже) | один из двух |
| `ManagerName` (Контактное лицо) | цепочка контактов (см. ниже) | без него — отказ публикации |
| `ContactPhone` (Телефон) | цепочка контактов | без него — отказ публикации |
| `Condition` | `product.condition` → «Новое»/«Б/у» | дефолт «Новое» |
| `Images` (≥1 фото) | публикуемые фото товара | → дефолтная картинка категории |

`SparePartType` (Вид запчасти) — обязателен (напр. «Автосвет», «Тормозная
система»). Avito определяет его сам по названию, если не передан; мы отдаём
его из `CategoryMapping.attributes_map['SparePartType']`, если задан.

## Цепочки разрешения (приоритет сверху вниз)

**Адрес / SellerAddressID** (`_add_placement`):
listing.placement_address → `*_override` листинга → bulk-поля →
`CategoryMapping.attributes_map` → дефолтный адрес аккаунта
(`placement_addresses` is_default) → `account.default_*`.
⚠️ Если `SellerAddressID` равен `external_id` аккаунта — игнорируется
(частая ошибка ввода ID аккаунта вместо ID адреса).

**Контакты** (`get_contact_fields`): та же цепочка для `manager_name` и
`contact_phone`. Пустые контакты → объявление не отправляется (отказ с
понятным текстом + Telegram).

## Служебные поля
- `AllowEmail` = «Нет» (хардкод).
- `AvitoStatus` / `AvitoDateEnd` — **не заполняем** (по требованию Avito).

## Снятие с публикации (важно)
В формате Avito **нет тега `<Status>Remove>`** (мы его раньше ошибочно слали — Avito его игнорировал, объявление оставалось активным). Правильно:
- **Снять одно объявление** — **не включать его в фид** (Avito архивирует то, чего нет в файле). `build_feed` отдаёт только активные; `_account_feed_listings` не включает archived/deleted.
- **Снять все** — команда **STOP**: `build_stop_feed()` → `<Ad><Id>STOP</Id></Ad>`. Нужна, когда активных не осталось (пустой фид снятием не считается). См. `AvitoAdapter.flush_stop()` и `_flush_account_or_stop()`.

## Источники данных (сводно)
- **Listing**: title, description_ai, price_on_listing, ad_type, override-поля размещения/контактов.
- **Product**: name, brand, article, oem_numbers, condition, description_1c, images.
- **CategoryMapping.attributes_map**: GoodsType, ProductType, адресные атрибуты — настройка на тенанта/категорию.
- **MarketplacePlacementAddress / MarketplaceAccount**: адреса и контакты по умолчанию.
- **Tenant**: name (fallback для Brand).

## Как это масштабируется на другие категории

Сейчас часть полей **захардкожена под запчасти** (`AdType`/`GoodsType` дефолты,
всегда отдаются `Brand`/`OEM`, `ProductType` из маппинга). Для других категорий
Avito набор обязательных полей другой (напр. «Масла» → объём, вязкость; «Шины» →
диаметр, сезон), и `Brand`/`OEM` могут быть неприменимы.

Текущая точка расширения — `CategoryMapping.attributes_map` (произвольные
ключ→значение, попадают в фид). Рекомендуемое развитие:

1. Описать обязательные/желательные поля **per Avito-категория** (схема), а не в коде.
2. `build_feed` собирает теги из этой схемы + `attributes_map`, без хардкода
   запчастёвых полей. Добавление категории = настройка маппинга, не правка кода.
3. Значения уровня товара (Brand, OEM, объём и т.д.) тянуть из полей Product
   по схеме категории; недостающие — из enrichment/ручного ввода, с
   предупреждением тенанту (как уже сделано для OEM).

Пока платформа автозапчастёвая — дефолты под «Запчасти» оправданы; при выходе
за пределы запчастей нужно вынести поля категории в конфиг (п. 1–3).
