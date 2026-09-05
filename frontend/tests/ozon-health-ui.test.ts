import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const component = readFileSync('src/components/marketplaces/OzonSyncHealth.tsx', 'utf8');
const settings = readFileSync('src/components/marketplaces/OzonAccountSettings.tsx', 'utf8');

test('Ozon diagnostics explain disabled, idle, error and delayed states separately', () => {
  for (const label of ['Выключено', 'Нет задач', 'Нет данных', 'Ожидаем результат', 'Успешный запуск', 'Ошибка', 'Задержка']) {
    assert.ok(component.includes(label), label);
  }
  assert.match(component, /Последний успешный запуск/);
  assert.match(component, /Повторы одного инцидента не отправляются/);
});

test('Ozon health refresh reads local diagnostics only and warns about stale snapshots', () => {
  assert.match(component, /accountApi\.getOzonHealth\(accountId\)/);
  assert.doesNotMatch(component, /accountApi\.(?:publish|archive|sync|update)/);
  assert.match(component, /Экран давно не обновлялся/);
  assert.match(component, /monitor_status !== 'ok'/);
  assert.match(component, /не отправляет товары, цены или остатки/);
  assert.match(component, /\/dashboard\/settings#notifications/);
});

test('health is scoped to the selected account and invalidated after changing switches', () => {
  assert.match(settings, /accountId=\{account\.id\}[\s\S]*initial=\{profile\?\.sync_health\}/);
  assert.match(settings, /\.\.\.flags, sync_health: undefined/);
});
