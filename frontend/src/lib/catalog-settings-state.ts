export interface CatalogCategoryState {
  id: number;
  parent: number | null;
  is_active: boolean;
}

export function catalogCategoryBranchIds<T extends CatalogCategoryState>(
  categories: readonly T[],
  rootId: number,
): Set<number> {
  const childrenByParent = new Map<number, number[]>();
  for (const category of categories) {
    if (category.parent === null) continue;
    const children = childrenByParent.get(category.parent) ?? [];
    children.push(category.id);
    childrenByParent.set(category.parent, children);
  }

  const branchIds = new Set<number>([rootId]);
  const queue = [rootId];
  while (queue.length > 0) {
    const categoryId = queue.pop();
    if (categoryId === undefined) break;
    for (const childId of childrenByParent.get(categoryId) ?? []) {
      if (branchIds.has(childId)) continue;
      branchIds.add(childId);
      queue.push(childId);
    }
  }
  return branchIds;
}

export function updateCatalogCategoryBranch<T extends CatalogCategoryState>(
  categories: readonly T[],
  rootId: number,
  isActive: boolean,
): T[] {
  const branchIds = catalogCategoryBranchIds(categories, rootId);
  return categories.map((category) => (
    branchIds.has(category.id)
      ? { ...category, is_active: isActive }
      : category
  ));
}
