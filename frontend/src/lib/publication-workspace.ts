import type { OzonOfferPreparation } from './ozon-offer-preparation';

export interface PublicationWorkspaceListing {
  id: number;
  account_id: number;
  status: string;
  status_display: string;
  can_publish: boolean;
  avito_field_errors?: Record<string, string[]>;
}

export type PublicationTargetTone = 'ready' | 'warning' | 'published' | 'neutral';

export interface PublicationTargetState {
  label: string;
  tone: PublicationTargetTone;
  issueCount: number;
  prepared: boolean;
}

export type PublicationWorkspaceView =
  | { kind: 'none' }
  | { kind: 'ozon' }
  | { kind: 'avito_setup' }
  | { kind: 'avito_listing'; listingId: number };

export function publicationWorkspaceView(
  account: { marketplace: string } | null,
  avitoListing: PublicationWorkspaceListing | null,
): PublicationWorkspaceView {
  if (!account) return { kind: 'none' };
  if (account.marketplace === 'ozon') return { kind: 'ozon' };
  if (account.marketplace !== 'avito') return { kind: 'none' };
  if (avitoListing) return { kind: 'avito_listing', listingId: avitoListing.id };
  return { kind: 'avito_setup' };
}

function messageCount(messages: Record<string, string[]> | undefined): number {
  return Object.values(messages ?? {}).reduce((total, values) => total + values.length, 0);
}

export function avitoTargetState(
  listing: PublicationWorkspaceListing | null,
): PublicationTargetState {
  if (!listing) {
    return {
      label: 'Не подготовлен',
      tone: 'neutral',
      issueCount: 0,
      prepared: false,
    };
  }
  if (listing.status === 'active') {
    return {
      label: 'Опубликован',
      tone: 'published',
      issueCount: 0,
      prepared: true,
    };
  }
  const issueCount = messageCount(listing.avito_field_errors);
  if (issueCount > 0) {
    return {
      label: `Нужно исправить: ${issueCount}`,
      tone: 'warning',
      issueCount,
      prepared: true,
    };
  }
  return {
    label: listing.can_publish ? 'Готов к отправке' : listing.status_display,
    tone: listing.can_publish ? 'ready' : 'neutral',
    issueCount: 0,
    prepared: true,
  };
}

export function ozonTargetState(
  preparation: OzonOfferPreparation | null,
): PublicationTargetState {
  if (!preparation?.draft) {
    return {
      label: 'Не подготовлен',
      tone: 'neutral',
      issueCount: preparation?.preflight.errors.length ?? 0,
      prepared: false,
    };
  }
  const issueCount = preparation.preflight.errors.length;
  if (issueCount > 0) {
    return {
      label: `Нужно исправить: ${issueCount}`,
      tone: 'warning',
      issueCount,
      prepared: true,
    };
  }
  if (!preparation.preflight.ready) {
    return {
      label: 'Подготовка не завершена',
      tone: 'neutral',
      issueCount: 0,
      prepared: true,
    };
  }
  return {
    label: 'Готов к будущей отправке',
    tone: 'ready',
    issueCount: 0,
    prepared: true,
  };
}

export function publicationTargetBadgeVariant(
  tone: PublicationTargetTone,
): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (tone === 'published' || tone === 'ready') return 'default';
  if (tone === 'warning') return 'destructive';
  return 'outline';
}
