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
