'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  Loader2,
} from 'lucide-react';
import { toast } from 'sonner';

import { MarketplaceChannelSwitcher } from '@/components/listings/MarketplaceChannelSwitcher';
import { OzonOfferPreparationCard } from '@/components/products/OzonOfferPreparation';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { accountApi, listingApi, productApi } from '@/lib/api';
import type { OzonOfferPreparation } from '@/lib/ozon-offer-preparation';
import {
  avitoTargetState,
  ozonTargetState,
  publicationWorkspaceView,
  type PublicationTargetState,
  type PublicationWorkspaceListing,
} from '@/lib/publication-workspace';

interface WorkspaceAccount {
  id: number;
  name: string;
  marketplace: string;
  marketplace_label: string;
  is_active: boolean;
}

interface ProductListingOption {
  id: number;
}

interface WorkspaceProduct {
  id: number;
  article: string;
  name: string;
  brand: string | null;
  listing_options: ProductListingOption[];
}

interface AvitoListingDetail extends PublicationWorkspaceListing {
  account_name: string;
  product_id: number;
}

interface Props {
  productId: number | null;
  selectedAccountId: number | null;
  onSelectedAccountChange: (accountId: number) => void;
  onOpenAvitoListing: (listingId: number) => void;
  onClose: () => void;
}

function envelopeData<T>(body: unknown): T {
  return (body as { data: T }).data;
}

interface WorkspaceSnapshot {
  product: WorkspaceProduct;
  accounts: WorkspaceAccount[];
  avitoListings: Record<number, AvitoListingDetail>;
  ozonPreparations: Record<number, OzonOfferPreparation>;
}

async function fetchPublicationWorkspace(
  requestedProductId: number,
): Promise<WorkspaceSnapshot> {
  const [productResponse, accountsResponse] = await Promise.all([
    productApi.get(requestedProductId),
    accountApi.list(),
  ]);
  const product = envelopeData<WorkspaceProduct>(productResponse.data);
  const accountResults = (
    accountsResponse.data.data ?? accountsResponse.data
  ) as WorkspaceAccount[];
  const accounts = accountResults.filter((account) => (
    account.is_active && ['avito', 'ozon'].includes(account.marketplace)
  ));
  const listingResponses = await Promise.all(
    product.listing_options.map((option) => (
      listingApi.get(option.id).then((response) => (
        envelopeData<AvitoListingDetail>(response.data)
      ))
    )),
  );
  const avitoListings: Record<number, AvitoListingDetail> = {};
  listingResponses.forEach((listing) => {
    if (listing.product_id === requestedProductId) {
      avitoListings[listing.account_id] = listing;
    }
  });
  const ozonAccounts = accounts.filter((account) => account.marketplace === 'ozon');
  const ozonResponses = await Promise.all(
    ozonAccounts.map((account) => (
      productApi.getOzonOffer(requestedProductId, account.id)
        .then((response) => ({
          accountId: account.id,
          preparation: envelopeData<OzonOfferPreparation>(response.data),
        }))
    )),
  );
  const ozonPreparations: Record<number, OzonOfferPreparation> = {};
  ozonResponses.forEach((result) => {
    ozonPreparations[result.accountId] = result.preparation;
  });

  return { product, accounts, avitoListings, ozonPreparations };
}

export default function PublicationWorkspaceDrawer({
  productId,
  selectedAccountId,
  onSelectedAccountChange,
  onOpenAvitoListing,
  onClose,
}: Props) {
  const [product, setProduct] = useState<WorkspaceProduct | null>(null);
  const [accounts, setAccounts] = useState<WorkspaceAccount[]>([]);
  const [avitoListings, setAvitoListings] = useState<Record<number, AvitoListingDetail>>({});
  const [ozonPreparations, setOzonPreparations] = useState<Record<number, OzonOfferPreparation>>({});
  const [loading, setLoading] = useState(productId !== null);
  const [loadError, setLoadError] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [preparingAccountId, setPreparingAccountId] = useState<number | null>(null);
  const open = productId !== null;

  useEffect(() => {
    if (productId === null) return;
    let cancelled = false;
    void fetchPublicationWorkspace(productId)
      .then((snapshot) => {
        if (cancelled) return;
        setProduct(snapshot.product);
        setAccounts(snapshot.accounts);
        setAvitoListings(snapshot.avitoListings);
        setOzonPreparations(snapshot.ozonPreparations);
        setLoadError(false);
      })
      .catch(() => {
        if (cancelled) return;
        setProduct(null);
        setAccounts([]);
        setAvitoListings({});
        setOzonPreparations({});
        setLoadError(true);
        toast.error('Не удалось открыть каналы публикации товара.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [productId, reloadToken]);

  function retryWorkspace() {
    setLoadError(false);
    setLoading(true);
    setReloadToken((current) => current + 1);
  }

  useEffect(() => {
    if (
      accounts.length > 0
      && !accounts.some((account) => account.id === selectedAccountId)
    ) {
      onSelectedAccountChange(accounts[0].id);
    }
  }, [accounts, onSelectedAccountChange, selectedAccountId]);

  const selectedAccount = useMemo(() => (
    accounts.find((account) => account.id === selectedAccountId) ?? null
  ), [accounts, selectedAccountId]);
  const selectedAvitoListing = selectedAccount?.marketplace === 'avito'
    ? avitoListings[selectedAccount.id] ?? null
    : null;
  const selectedView = useMemo(() => (
    publicationWorkspaceView(selectedAccount, selectedAvitoListing)
  ), [selectedAccount, selectedAvitoListing]);
  const accountStates = useMemo<Record<number, PublicationTargetState>>(() => (
    Object.fromEntries(accounts.map((account) => [
      account.id,
      account.marketplace === 'avito'
        ? avitoTargetState(avitoListings[account.id] ?? null)
        : ozonTargetState(ozonPreparations[account.id] ?? null),
    ]))
  ), [accounts, avitoListings, ozonPreparations]);

  useEffect(() => {
    if (selectedView.kind !== 'avito_listing') return;
    onOpenAvitoListing(selectedView.listingId);
  }, [onOpenAvitoListing, selectedView]);

  const handleOzonPreparationChange = useCallback((next: OzonOfferPreparation | null) => {
    if (!selectedAccount || selectedAccount.marketplace !== 'ozon') return;
    setOzonPreparations((current) => {
      const updated = { ...current };
      if (next) updated[selectedAccount.id] = next;
      else delete updated[selectedAccount.id];
      return updated;
    });
  }, [selectedAccount]);

  async function prepareAvito(account: WorkspaceAccount) {
    if (!product) return;
    setPreparingAccountId(account.id);
    try {
      const response = await productApi.publish(product.id, [account.id]);
      const listingId = Number(response.data.data?.listing_ids?.[0]);
      if (!Number.isInteger(listingId) || listingId <= 0) {
        throw new Error('Missing listing id');
      }
      toast.success(`Черновик Avito для «${account.name}» подготовлен.`);
      onOpenAvitoListing(listingId);
    } catch (error: unknown) {
      const message = (
        error as { response?: { data?: { message?: string } } }
      ).response?.data?.message;
      toast.error(message ?? 'Не удалось подготовить черновик Avito.');
    } finally {
      setPreparingAccountId(null);
    }
  }

  return (
    <Sheet open={open} onOpenChange={(nextOpen) => { if (!nextOpen) onClose(); }}>
      <SheetContent
        side="right"
        className="h-[100dvh] w-[100dvw] min-w-0 max-w-[100dvw] overflow-y-auto p-0 sm:max-w-[100dvw] xl:w-[min(94vw,1280px)] xl:max-w-[min(94vw,1280px)]"
      >
        {loading ? (
          <div className="flex h-full items-center justify-center gap-2 text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin" /> Открываем каналы публикации…
          </div>
        ) : loadError || !product ? (
          <div className="flex h-full items-center justify-center p-6">
            <div className="max-w-md rounded-lg border bg-background p-5 text-center">
              <AlertCircle className="mx-auto h-6 w-6 text-destructive" />
              <p className="mt-3 font-medium">Не удалось загрузить каналы</p>
              <p className="mt-1 text-sm text-muted-foreground">
                MAP не будет показывать непроверенный статус как
                «не подготовлен». Повторите локальную проверку.
              </p>
              <div className="mt-4 flex justify-center gap-2">
                <Button variant="outline" onClick={onClose}>Закрыть</Button>
                <Button onClick={retryWorkspace}>Повторить</Button>
              </div>
            </div>
          </div>
        ) : (
          <div className="min-h-full bg-muted/15 p-4 pb-10 sm:p-6">
            <SheetHeader className="pr-10 text-left">
              <div className="flex flex-wrap items-center gap-2">
                <SheetTitle>Листинг товара</SheetTitle>
                <Badge variant="outline">{product.article}</Badge>
              </div>
              <p className="text-sm text-muted-foreground">{product.name}</p>
            </SheetHeader>

            {accounts.length === 0 ? (
              <div className="mt-5 rounded-lg border bg-background p-5 text-sm">
                <p>Нет активных кабинетов маркетплейсов.</p>
                <Button asChild className="mt-3" variant="outline">
                  <Link href="/dashboard/settings#marketplaces">Открыть настройки</Link>
                </Button>
              </div>
            ) : (
              <div className="mt-5 space-y-5">
                <div className="rounded-lg border bg-background p-4">
                  <MarketplaceChannelSwitcher
                    accounts={accounts}
                    selectedAccountId={selectedAccount?.id ?? null}
                    states={accountStates}
                    onSelect={onSelectedAccountChange}
                  />
                </div>

                <main className="min-w-0">
                  {selectedAccount && (
                    selectedView.kind === 'avito_listing' || selectedView.kind === 'avito_setup'
                  ) ? (
                    <div className="rounded-lg border bg-background p-4 sm:p-5">
                      {selectedView.kind === 'avito_listing' ? (
                        <div className="flex items-center gap-2 text-sm text-muted-foreground">
                          <Loader2 className="h-4 w-4 animate-spin" />
                          Открываем данные Avito без промежуточного экрана…
                        </div>
                      ) : (
                        <>
                          <p className="text-sm font-medium">Avito · {selectedAccount.name}</p>
                          <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                            Для этого кабинета ещё нет черновика. MAP создаст его
                            через текущую проверенную логику Avito, без немедленной отправки.
                          </p>
                          <Button
                            className="mt-4"
                            onClick={() => prepareAvito(selectedAccount)}
                            disabled={preparingAccountId === selectedAccount.id}
                          >
                            {preparingAccountId === selectedAccount.id && (
                              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            )}
                            Подготовить черновик Avito
                          </Button>
                        </>
                      )}
                    </div>
                  ) : selectedView.kind === 'ozon' && selectedAccount ? (
                    <div className="space-y-3">
                      <div className="rounded-lg border border-blue-500/20 bg-blue-500/5 p-3 text-sm text-muted-foreground">
                        Это отдельная проекция Ozon для кабинета «{selectedAccount.name}».
                        Она не меняет поля, дерево, наценки или фид Avito.
                      </div>
                      <OzonOfferPreparationCard
                        key={`${product.id}:${selectedAccount.id}`}
                        productId={product.id}
                        accounts={[selectedAccount]}
                        onPreparationChange={handleOzonPreparationChange}
                        showAccountSelector={false}
                      />
                    </div>
                  ) : null}
                </main>
              </div>
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
