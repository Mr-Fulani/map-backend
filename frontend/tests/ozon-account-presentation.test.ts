import assert from 'node:assert/strict';
import test from 'node:test';

import type { OzonAccountProfile } from '../src/lib/marketplace-account-types';
import {
  ozonAccountConnectionEnabled,
  ozonConnectionPresentation,
  ozonCredentialUpdateEnabled,
  ozonKeyExpiryPresentation,
  ozonOnboardingErrorMessage,
} from '../src/lib/ozon-account-presentation';

const connectedProfile: OzonAccountProfile = {
  connection_status: 'connected',
  company_name: 'АльфаПро',
  seller_name: 'Alfa Seller',
  currency: 'RUB',
  roles: ['Product API', 'FBS'],
  api_methods: [],
  api_key_expires_at: null,
  warehouse_count: 1,
  selected_warehouse_id: 'warehouse-1',
  selected_warehouse_name: 'Основной',
  last_checked_at: '2026-08-29T12:00:00Z',
};

test('Ozon connection presentation keeps warehouse states provider-specific', () => {
  assert.equal(ozonConnectionPresentation(connectedProfile).tone, 'success');
  assert.equal(ozonConnectionPresentation({
    ...connectedProfile,
    connection_status: 'warehouse_missing',
    warehouse_count: 0,
    selected_warehouse_id: '',
    selected_warehouse_name: '',
  }).tone, 'danger');
  const selection = ozonConnectionPresentation({
    ...connectedProfile,
    connection_status: 'warehouse_selection_required',
    warehouse_count: 2,
    selected_warehouse_id: '',
    selected_warehouse_name: '',
  });
  assert.equal(selection.tone, 'warning');
  assert.match(selection.description, /2/);
});

test('Ozon key expiry uses the exact provider timestamp', () => {
  const now = Date.parse('2026-08-01T00:00:00Z');
  assert.equal(
    ozonKeyExpiryPresentation('2026-09-01T00:00:00Z', now).tone,
    'success',
  );
  assert.equal(
    ozonKeyExpiryPresentation('2026-08-10T00:00:00Z', now).tone,
    'warning',
  );
  assert.equal(
    ozonKeyExpiryPresentation('2026-07-31T00:00:00Z', now).tone,
    'danger',
  );
  assert.equal(ozonKeyExpiryPresentation(null, now).tone, 'neutral');
});

test('Ozon rollout parsing fails closed unless backend returns literal true', () => {
  const enabled = {
    ozon: {
      account_connection_enabled: true,
      credential_update_enabled: true,
    },
  };
  assert.equal(ozonAccountConnectionEnabled(enabled), true);
  assert.equal(ozonCredentialUpdateEnabled(enabled), true);
  assert.equal(ozonAccountConnectionEnabled({ ozon: { account_connection_enabled: 'true' } }), false);
  assert.equal(ozonCredentialUpdateEnabled({}), false);
  assert.equal(ozonAccountConnectionEnabled(null), false);
});

test('Ozon onboarding errors are allowlisted and never reflect unknown response text', () => {
  assert.match(ozonOnboardingErrorMessage({
    response: { data: { code: 'invalid_credentials' } },
  }), /Client ID/);
  assert.match(ozonOnboardingErrorMessage({
    response: { data: { code: 'rate_limited', retry_after_seconds: 11 } },
  }), /11 сек/);

  const unknown = ozonOnboardingErrorMessage({
    response: {
      status: 500,
      data: { code: 'unknown', message: 'api-key-must-not-leak' },
    },
  });
  assert.doesNotMatch(unknown, /api-key-must-not-leak/);
  assert.match(unknown, /API-ключ не сохранён/);
});
