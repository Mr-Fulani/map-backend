from decimal import Decimal
from typing import Any

from django.db import transaction

from apps.marketplaces.models import MarketplaceAccount, OzonAccountProfile, OzonOfferDraft
from apps.marketplaces.ozon_catalog import OzonCatalogService
from apps.products.models import Product, ProductImage
from apps.products.physical_profiles import physical_profile_presentation


class OzonOfferError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


PHYSICAL_LABELS = {
    'barcode': 'Штрихкод',
    'length_mm': 'Длина',
    'width_mm': 'Ширина',
    'height_mm': 'Высота',
    'weight_g': 'Вес',
    'vat_rate': 'НДС',
}


def _issue(code: str, field: str, label: str, message: str) -> dict[str, str]:
    return {'code': code, 'field': field, 'label': label, 'message': message}


def _preflight(
    product: Product,
    account: MarketplaceAccount,
    draft: OzonOfferDraft | None,
) -> dict[str, Any]:
    errors: list[dict[str, str]] = []
    recommendations: list[dict[str, str]] = []
    profile = OzonAccountProfile.objects.filter(account=account).first()
    if not account.is_active:
        errors.append(_issue('account_inactive', 'account', 'Аккаунт', 'Аккаунт Ozon выключен.'))
    if profile is None or profile.connection_status != OzonAccountProfile.ConnectionStatus.CONNECTED:
        errors.append(_issue(
            'account_not_ready', 'account', 'Аккаунт',
            'Сначала проверьте подключение аккаунта Ozon.',
        ))
    elif not profile.selected_warehouse_id:
        errors.append(_issue(
            'warehouse_missing', 'warehouse', 'Склад',
            'В аккаунте Ozon не выбран FBS-склад.',
        ))

    if draft is None:
        errors.append(_issue(
            'draft_missing', 'offer_id', 'Черновик Ozon',
            'Начните подготовку товара для выбранного аккаунта.',
        ))
    elif draft.description_category_id is None or draft.type_id is None:
        errors.append(_issue(
            'category_missing', 'category', 'Категория Ozon',
            'Выберите конечную категорию и тип товара Ozon.',
        ))
    else:
        tree, types = OzonCatalogService.category_types(account)
        current_type = next((item for item in types if (
            item['description_category_id'] == draft.description_category_id
            and item['type_id'] == draft.type_id
        )), None)
        if tree is None or current_type is None:
            errors.append(_issue(
                'category_outdated', 'category', 'Категория Ozon',
                'Категория отсутствует в последнем локальном снимке Ozon.',
            ))
        elif draft.tree_revision != tree.schema_hash:
            errors.append(_issue(
                'tree_revision_outdated', 'category', 'Категория Ozon',
                'Дерево Ozon обновилось — подтвердите категорию ещё раз.',
            ))

    physical = physical_profile_presentation(product)
    for field in physical['missing_fields']:
        errors.append(_issue(
            'physical_fact_missing', f'physical:{field}', PHYSICAL_LABELS[field],
            'Заполните значение из 1С или MAP в блоке «Данные для Ozon».',
        ))
    if not (product.title_ai or product.name).strip():
        errors.append(_issue('name_missing', 'name', 'Название', 'У товара нет названия.'))
    if not (product.brand or '').strip():
        errors.append(_issue('brand_missing', 'brand', 'Бренд', 'Укажите бренд товара.'))
    if Decimal(product.price) <= 0:
        errors.append(_issue('price_missing', 'price', 'Цена', 'Цена должна быть больше нуля.'))
    if not product.images.exclude(status=ProductImage.Status.REJECTED).exists():
        errors.append(_issue('image_missing', 'images', 'Фотографии', 'Добавьте хотя бы одну фотографию.'))
    if not (product.description_ai or '').strip():
        recommendations.append(_issue(
            'description_recommended', 'description', 'Описание',
            'Добавьте описание — карточка будет понятнее покупателю.',
        ))
    if product.stock_qty == 0:
        recommendations.append(_issue(
            'stock_zero', 'stock', 'Остаток',
            'Остаток равен нулю: после будущей публикации товар не появится в продаже.',
        ))
    return {'ready': not errors, 'errors': errors, 'recommendations': recommendations}


def offer_presentation(product: Product, account: MarketplaceAccount) -> dict[str, Any]:
    draft = OzonOfferDraft.objects.filter(
        tenant=product.tenant,
        product=product,
        account=account,
    ).first()
    return {
        'account': {'id': account.pk, 'name': account.name, 'marketplace': 'ozon'},
        'draft': None if draft is None else {
            'id': draft.pk,
            'offer_id': draft.offer_id,
            'category': None if draft.description_category_id is None else {
                'description_category_id': draft.description_category_id,
                'type_id': draft.type_id,
                'category_path': draft.category_path,
                'type_name': draft.type_name,
                'tree_revision': draft.tree_revision,
            },
            'updated_at': draft.updated_at,
        },
        'preflight': _preflight(product, account, draft),
    }


@transaction.atomic
def update_offer_draft(
    product: Product,
    account: MarketplaceAccount,
    *,
    category: tuple[int, int] | None = None,
) -> OzonOfferDraft:
    if (
        product.tenant_id != account.tenant_id
        or account.marketplace != MarketplaceAccount.MARKETPLACE_OZON
    ):
        raise OzonOfferError(
            'account_scope_mismatch',
            'Товар и аккаунт Ozon должны принадлежать одному tenant-у.',
        )
    Product.objects.select_for_update().get(pk=product.pk, tenant=product.tenant)
    draft = OzonOfferDraft.objects.select_for_update().filter(
        tenant=product.tenant,
        product=product,
        account=account,
    ).first()
    if draft is None:
        draft = OzonOfferDraft(tenant=product.tenant, product=product, account=account)

    if category is not None:
        tree, types = OzonCatalogService.category_types(account)
        selected = next((item for item in types if (
            item['description_category_id'] == category[0] and item['type_id'] == category[1]
        )), None)
        if tree is None or selected is None:
            raise OzonOfferError(
                'invalid_category_type',
                'Выберите конечную категорию из последнего снимка Ozon.',
            )
        draft.description_category_id = category[0]
        draft.type_id = category[1]
        draft.category_path = selected['category_path']
        draft.type_name = selected['type_name']
        draft.tree_revision = tree.schema_hash
    draft.save()
    return draft
