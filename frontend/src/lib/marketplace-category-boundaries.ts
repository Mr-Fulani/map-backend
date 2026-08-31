export const MAP_CATALOG_LABEL = 'Каталог MAP';
export const AVITO_PRICING_LABEL = 'Наценки Avito';
export const OZON_CATEGORIES_LABEL = 'Категории Ozon';
export const OZON_PRICING_LABEL = 'Наценки Ozon';

export function mapCatalogCategorySourceLabel(source: string): string {
  if (source === 'avito') return 'Официальные категории Avito в Каталоге MAP';
  if (source) return MAP_CATALOG_LABEL;
  return 'Собственные категории MAP';
}

export function canEditMapCatalogStructure(source: string): boolean {
  return source !== 'avito';
}
