import assert from 'node:assert/strict';
import test from 'node:test';

import {
  avitoTargetState,
  ozonTargetState,
  publicationTargetBadgeVariant,
  publicationWorkspaceView,
} from '../src/lib/publication-workspace';
import type { OzonOfferPreparation } from '../src/lib/ozon-offer-preparation';


test('Avito readiness remains based on its existing listing preflight', () => {
  assert.deepEqual(avitoTargetState(null), {
    label: 'Не подготовлен',
    tone: 'neutral',
    issueCount: 0,
    prepared: false,
  });
  assert.deepEqual(avitoTargetState({
    id: 7,
    account_id: 4,
    status: 'draft',
    status_display: 'Черновик',
    can_publish: true,
    avito_field_errors: {
      product_brand: ['Укажите бренд'],
      images: ['Добавьте фото'],
    },
  }), {
    label: 'Нужно исправить: 2',
    tone: 'warning',
    issueCount: 2,
    prepared: true,
  });
});

test('published Avito target is independent from another marketplace', () => {
  const state = avitoTargetState({
    id: 8,
    account_id: 4,
    status: 'active',
    status_display: 'Активно',
    can_publish: false,
  });
  assert.equal(state.label, 'Опубликован');
  assert.equal(state.tone, 'published');
});

test('an existing Avito draft opens the full Avito view without an intermediate summary', () => {
  const listing = {
    id: 21,
    account_id: 4,
    status: 'draft',
    status_display: 'Черновик',
    can_publish: true,
  };

  assert.deepEqual(
    publicationWorkspaceView({ marketplace: 'avito' }, listing),
    { kind: 'avito_listing', listingId: 21 },
  );
  assert.deepEqual(
    publicationWorkspaceView({ marketplace: 'avito' }, null),
    { kind: 'avito_setup' },
  );
});

test('Ozon stays in the provider workspace even when Avito has a draft', () => {
  const listing = {
    id: 22,
    account_id: 4,
    status: 'active',
    status_display: 'Активно',
    can_publish: false,
  };

  assert.deepEqual(
    publicationWorkspaceView({ marketplace: 'ozon' }, listing),
    { kind: 'ozon' },
  );
});

test('Ozon target reports exact account draft readiness', () => {
  const preparation = {
    account: { id: 15, name: 'Ozon 1', marketplace: 'ozon' },
    draft: {
      id: 3,
      offer_id: 'map-offer',
      category: null,
      attribute_schema_revision: '',
      updated_at: '2026-08-30T00:00:00Z',
    },
    attributes: [],
    schema: null,
    pricing: null,
    autofill: {
      status: 'not_started',
      updated_at: null,
      moderated_at: null,
      applied_count: 0,
      preserved_count: 0,
      fields: {},
      recommendations: [],
    },
    preflight: {
      ready: false,
      errors: [
        { code: 'category_missing', field: 'category', label: 'Категория', message: 'Выберите' },
      ],
      recommendations: [],
    },
  } satisfies OzonOfferPreparation;
  assert.deepEqual(ozonTargetState(preparation), {
    label: 'Нужно исправить: 1',
    tone: 'warning',
    issueCount: 1,
    prepared: true,
  });
  assert.equal(publicationTargetBadgeVariant('warning'), 'destructive');
  assert.equal(publicationTargetBadgeVariant('ready'), 'default');
});

test('Ozon target does not infer readiness from an empty error list', () => {
  const preparation = {
    account: { id: 16, name: 'Ozon 2', marketplace: 'ozon' },
    draft: {
      id: 4,
      offer_id: 'map-offer-2',
      category: null,
      attribute_schema_revision: '',
      updated_at: '2026-08-30T00:00:00Z',
    },
    attributes: [],
    schema: null,
    pricing: null,
    autofill: {
      status: 'not_started',
      updated_at: null,
      moderated_at: null,
      applied_count: 0,
      preserved_count: 0,
      fields: {},
      recommendations: [],
    },
    preflight: {
      ready: false,
      errors: [],
      recommendations: [],
    },
  } satisfies OzonOfferPreparation;

  assert.deepEqual(ozonTargetState(preparation), {
    label: 'Подготовка не завершена',
    tone: 'neutral',
    issueCount: 0,
    prepared: true,
  });
});
