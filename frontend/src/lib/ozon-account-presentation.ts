import type { OzonAccountProfile } from './marketplace-account-types';

export type OzonStatusTone = 'success' | 'warning' | 'danger' | 'neutral';

export interface OzonStatusPresentation {
  label: string;
  description: string;
  tone: OzonStatusTone;
}

export function ozonConnectionPresentation(
  profile: OzonAccountProfile | null,
): OzonStatusPresentation {
  if (!profile) {
    return {
      label: 'Проверка не завершена',
      description: 'Нет безопасного снимка ролей, срока ключа и FBS-склада.',
      tone: 'neutral',
    };
  }
  if (profile.connection_status === 'connected') {
    return {
      label: 'Подключение проверено',
      description: 'Client ID, API-права и единственный FBS-склад подтверждены Ozon.',
      tone: 'success',
    };
  }
  if (profile.connection_status === 'warehouse_missing') {
    return {
      label: 'FBS-склад не найден',
      description: 'Создайте или активируйте FBS-склад в кабинете Ozon и повторите проверку ключа.',
      tone: 'danger',
    };
  }
  return {
    label: 'Нужно выбрать FBS-склад',
    description: `Ozon вернул несколько складов (${profile.warehouse_count}). MAP не выбирает склад автоматически.`,
    tone: 'warning',
  };
}

export function ozonKeyExpiryPresentation(
  expiresAt: string | null,
  nowMs = Date.now(),
): OzonStatusPresentation {
  if (!expiresAt) {
    return {
      label: 'Срок ключа не передан',
      description: 'MAP покажет точный срок, когда Ozon вернёт expires_at.',
      tone: 'neutral',
    };
  }
  const expiresMs = Date.parse(expiresAt);
  if (!Number.isFinite(expiresMs)) {
    return {
      label: 'Срок ключа неизвестен',
      description: 'Ozon вернул дату в неподдерживаемом формате.',
      tone: 'warning',
    };
  }
  const daysLeft = Math.ceil((expiresMs - nowMs) / 86_400_000);
  if (daysLeft < 0) {
    return {
      label: 'API-ключ истёк',
      description: 'Создайте новый ключ в Ozon и выполните безопасное обновление credentials.',
      tone: 'danger',
    };
  }
  if (daysLeft === 0) {
    return {
      label: 'API-ключ истекает сегодня',
      description: 'Обновите ключ до следующей проверки подключения.',
      tone: 'danger',
    };
  }
  return {
    label: `API-ключ: ${daysLeft} дн.`,
    description: `Истекает ${new Date(expiresMs).toLocaleDateString('ru-RU')}.`,
    tone: daysLeft <= 14 ? 'warning' : 'success',
  };
}

export function ozonAccountConnectionEnabled(payload: unknown): boolean {
  if (!payload || typeof payload !== 'object' || !('ozon' in payload)) return false;
  const ozon = (payload as { ozon?: unknown }).ozon;
  return Boolean(
    ozon
    && typeof ozon === 'object'
    && 'account_connection_enabled' in ozon
    && (ozon as { account_connection_enabled?: unknown }).account_connection_enabled === true,
  );
}

export function ozonCredentialUpdateEnabled(payload: unknown): boolean {
  if (!payload || typeof payload !== 'object' || !('ozon' in payload)) return false;
  const ozon = (payload as { ozon?: unknown }).ozon;
  return Boolean(
    ozon
    && typeof ozon === 'object'
    && 'credential_update_enabled' in ozon
    && (ozon as { credential_update_enabled?: unknown }).credential_update_enabled === true,
  );
}

export function ozonOnboardingErrorMessage(error: unknown): string {
  const response = (error as {
    response?: {
      status?: number;
      data?: { code?: unknown; retry_after_seconds?: unknown };
    };
  } | null)?.response;
  const code = typeof response?.data?.code === 'string' ? response.data.code : '';

  if (code === 'provider_disabled') {
    return 'Подключение Ozon пока закрыто для текущего этапа rollout.';
  }
  if (code === 'account_exists') {
    return 'Этот кабинет Ozon уже подключён. Для смены ключа используйте обновление credentials.';
  }
  if (code === 'invalid_credentials') {
    return 'Ozon отклонил Client ID или API-ключ. Проверьте ключ и его роли.';
  }
  if (code === 'rate_limited') {
    const retryAfter = response?.data?.retry_after_seconds;
    return Number.isSafeInteger(retryAfter) && Number(retryAfter) > 0
      ? `Ozon ограничил частоту запросов. Повторите через ${retryAfter} сек.`
      : 'Ozon ограничил частоту запросов. Повторите позже.';
  }
  if (code === 'provider_unavailable' || code === 'connection_error') {
    return 'Ozon Seller API временно недоступен. Credentials не сохранены; повторите позже.';
  }
  if (
    code === 'invalid_response'
    || code === 'page_limit_exceeded'
    || code === 'request_rejected'
  ) {
    return 'Ozon не подтвердил подключение безопасным ответом. Credentials не сохранены.';
  }
  if (code === 'validation_error' || response?.status === 400) {
    return 'Проверьте Client ID, API-ключ и обязательные поля подключения.';
  }
  if (response?.status === 403) {
    return 'Подключать маркетплейсы может только владелец или администратор тенанта.';
  }
  return 'Не удалось проверить подключение Ozon. API-ключ не сохранён.';
}
