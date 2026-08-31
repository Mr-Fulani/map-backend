export const MAP_CATALOG_LABEL = 'Каталог товаров';
export const MARKETPLACE_CATEGORIES_LABEL = 'Категории площадок';
export const MARKETPLACE_PRICING_LABEL = 'Правила цены';
export const AVITO_CATEGORIES_LABEL = 'Справочник категорий Avito';
export const OZON_CATEGORIES_LABEL = 'Справочник категорий Ozon';
export const AVITO_PRICING_LABEL = 'Правила цены Avito';
export const OZON_PRICING_LABEL = 'Правила цены Ozon';

export function mapCatalogCategorySourceLabel(source: string): string {
  if (source === 'avito') return 'Защищённая основа Avito';
  if (source) return MAP_CATALOG_LABEL;
  return 'Собственная категория';
}

export function canEditMapCatalogStructure(source: string): boolean {
  return source !== 'avito';
}
