import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test from 'node:test';

function source(path: string): string {
  return readFileSync(resolve(process.cwd(), path), 'utf8');
}

test('Ozon desktop workspace keeps market audit left and editable card right', () => {
  const workspace = source('src/components/listings/PublicationWorkspaceDrawer.tsx');
  const marketPanel = workspace.indexOf('data-testid="ozon-market-audit-panel"');
  const editorPanel = workspace.indexOf('data-testid="ozon-listing-editor-panel"');

  assert.ok(marketPanel >= 0, 'Ozon market audit panel must remain explicit');
  assert.ok(editorPanel > marketPanel, 'Ozon editor must remain the right-hand desktop panel');
  assert.match(
    workspace,
    /xl:grid-cols-\[minmax\(600px,1fr\)_minmax\(520px,560px\)\]/,
  );
  assert.match(workspace, /<MarketPricingPanel[\s\S]*channelLabel="Ozon"/);
  assert.match(workspace, /<OzonListingEditorPanel/);
});

test('Avito publication remains delegated to the protected legacy drawer', () => {
  const workspace = source('src/components/listings/PublicationWorkspaceDrawer.tsx');
  const avitoDrawer = source('src/components/listings/ListingDrawer.tsx');

  assert.match(workspace, /selectedView\.kind !== 'avito_listing'/);
  assert.match(workspace, /onOpenAvitoListing\(selectedView\.listingId\)/);
  assert.match(avitoDrawer, /await listingApi\.publish\(listing\.id\)/);
  assert.match(avitoDrawer, /channelLabel="Avito"/);
});

test('marketplace settings and Ozon editors stay outside the large page shells', () => {
  const settingsPage = source('src/app/dashboard/settings/page.tsx');
  const ozonPreparation = source('src/components/products/OzonOfferPreparation.tsx');

  assert.match(settingsPage, /<MarketplaceCatalogSettingsSections/);
  assert.doesNotMatch(settingsPage, /function MarginEditor/);
  assert.match(ozonPreparation, /<OzonOfferPricingEditor/);
  assert.match(ozonPreparation, /<OzonOfferAttributeEditor/);
});

test('successful Ozon lifecycle changes refresh the provider-neutral listing index', () => {
  const listingsPage = source('src/app/dashboard/listings/page.tsx');
  const workspace = source('src/components/listings/PublicationWorkspaceDrawer.tsx');

  assert.match(listingsPage, /<PublicationWorkspaceDrawer[\s\S]*onChannelChanged=\{load\}/);
  assert.equal(
    workspace.match(/await onChannelChanged\(\);/g)?.length,
    4,
    'publish, archive, reconcile and commerce sync must each refresh the channel row',
  );
});

test('publication workspace uses one bounded snapshot and loads only the selected Ozon card', () => {
  const workspace = source('src/components/listings/PublicationWorkspaceDrawer.tsx');
  const editor = source('src/components/listings/OzonListingEditorPanel.tsx');

  assert.match(workspace, /listingApi\.workspace\(requestedProductId\)/);
  assert.doesNotMatch(workspace, /product\.listing_options\.map/);
  assert.doesNotMatch(workspace, /ozonAccounts\.map[\s\S]*getOzonOffer/);
  assert.match(editor, /<OzonOfferPreparationCard/);
});

test('Ozon drawer mirrors Avito navigation cues without entering Avito publication code', () => {
  const editor = source('src/components/listings/OzonListingEditorPanel.tsx');
  const productPage = source('src/app/dashboard/products/[id]/page.tsx');
  const preparation = source('src/components/products/OzonOfferPreparation.tsx');
  const media = source('src/components/listings/ProductMediaManager.tsx');
  const pricing = source('src/components/listings/OzonListingPriceEditor.tsx');
  const physical = source('src/components/listings/ProductPhysicalProfileEditor.tsx');

  assert.match(editor, /sticky bottom-0/);
  assert.match(editor, /Нажмите на пункт — MAP прокрутит к нужному разделу/);
  assert.match(media, /Одни и те же фотографии используются в Avito, Ozon/);
  assert.match(media, /Загрузить фото/);
  assert.match(media, /Одобрить/);
  assert.match(media, /Отклонить/);
  assert.match(media, /Сделать главным/);
  assert.match(media, /Удалить эту фотографию/);
  assert.match(pricing, /Кабинет Ozon/);
  assert.match(pricing, /Наценка Ozon/);
  assert.match(pricing, /Цена объявления Ozon/);
  assert.match(pricing, /Если рассчитанная цена подходит, ничего менять не нужно/);
  assert.match(pricing, /aria-invalid/);
  assert.match(physical, /Упаковка и налог/);
  assert.match(physical, /productApi\.updatePhysicalProfile/);
  assert.match(physical, /Принять значение/);
  assert.match(physical, /Нужно получить или измерить/);
  assert.match(physical, /Проверить в источнике/);
  assert.match(productPage, /<ProductPhysicalProfileEditor/);
  assert.doesNotMatch(productPage, /setPhysicalDraftField/);
  assert.match(editor, /data-testid="ozon-guided-workflow"/);
  assert.match(editor, /Все обязательные данные редактируются прямо/);
  assert.match(editor, /Заголовок и описание заполняет обогащение/);
  assert.match(editor, /onSaveBrand/);
  assert.match(editor, /Сохранить характеристики и проверить/);
  assert.match(editor, /data-testid="ozon-barcode-workflow"/);
  assert.match(editor, /Создать штрихкод Ozon/);
  assert.match(editor, /showPricing={false}/);
  assert.match(editor, /showReadinessSummary={false}/);
  assert.match(preparation, /focusField/);
  assert.doesNotMatch(preparation, /getOzonCatalogTreeLevel\(accountId, \[\]\)/);
});

test('Ozon editable column follows the same user order as Avito', () => {
  const editor = source('src/components/listings/OzonListingEditorPanel.tsx');
  const media = editor.indexOf('<ProductMediaManager');
  const commonData = editor.indexOf('data-testid="ozon-common-product-section"');
  const pricing = editor.lastIndexOf('<OzonListingPriceEditor');
  const physical = editor.lastIndexOf('<ProductPhysicalProfileEditor');
  const providerFields = editor.lastIndexOf('<OzonOfferPreparationCard');
  const actions = editor.indexOf('Действия с карточкой Ozon');

  assert.ok(media >= 0, 'media moderation must be available inside the Ozon drawer');
  assert.ok(commonData > media, 'common enriched data must follow media');
  assert.ok(pricing > commonData, 'account and price must follow common data');
  assert.ok(physical > pricing, 'packaging fields must remain visible after price');
  assert.ok(providerFields > physical, 'Ozon category and attributes must follow common facts');
  assert.ok(actions > providerFields, 'save and publication actions must remain last');
});

test('Ozon media actions use the existing tenant-scoped image API', () => {
  const workspace = source('src/components/listings/PublicationWorkspaceDrawer.tsx');

  assert.match(workspace, /imageApi\.upload\(product\.id, file\)/);
  assert.match(workspace, /imageApi\.approve\(product\.id, imageId\)/);
  assert.match(workspace, /imageApi\.reject\(product\.id, imageId\)/);
  assert.match(workspace, /imageApi\.setPrimary\(product\.id, imageId\)/);
  assert.match(workspace, /imageApi\.delete\(product\.id, imageId\)/);
  assert.match(workspace, /<BarChart3[^>]*\/> Цены/);
});
