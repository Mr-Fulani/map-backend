'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  BarChart3,
  FileText,
  Loader2,
} from 'lucide-react';
import { toast } from 'sonner';

import { MarketplaceChannelSwitcher } from '@/components/listings/MarketplaceChannelSwitcher';
import MarketPricingPanel from '@/components/listings/MarketPricingPanel';
import { OzonListingEditorPanel } from '@/components/listings/OzonListingEditorPanel';
import type { OzonOfferPreparationCardHandle } from '@/components/products/OzonOfferPreparation';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';
import { accountApi, imageApi, listingApi, productApi } from '@/lib/api';
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
  price: string;
  stock_qty: number;
  title_ai: string;
  description_ai: string;
  listing_options: ProductListingOption[];
}

interface WorkspaceImage {
  id: number;
  status: string;
  is_primary: boolean;
  position: number;
  url: string;
  thumb_url: string;
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
  images: WorkspaceImage[];
  avitoListings: Record<number, AvitoListingDetail>;
  ozonPreparations: Record<number, OzonOfferPreparation>;
}

async function fetchPublicationWorkspace(
  requestedProductId: number,
): Promise<WorkspaceSnapshot> {
  const [productResponse, accountsResponse, imagesResponse] = await Promise.all([
    productApi.get(requestedProductId),
    accountApi.list(),
    imageApi.list(requestedProductId).catch(() => null),
  ]);
  const product = envelopeData<WorkspaceProduct>(productResponse.data);
  const accountResults = (
    accountsResponse.data.data ?? accountsResponse.data
  ) as WorkspaceAccount[];
  const accounts = accountResults.filter((account) => (
    account.is_active && ['avito', 'ozon'].includes(account.marketplace)
  ));
  const images = (imagesResponse
    ? envelopeData<WorkspaceImage[]>(imagesResponse.data)
    : [])
    .slice()
    .sort((left, right) => (
      Number(right.is_primary) - Number(left.is_primary)
      || left.position - right.position
      || left.id - right.id
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

  return { product, accounts, images, avitoListings, ozonPreparations };
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
  const [images, setImages] = useState<WorkspaceImage[]>([]);
  const [avitoListings, setAvitoListings] = useState<Record<number, AvitoListingDetail>>({});
  const [ozonPreparations, setOzonPreparations] = useState<Record<number, OzonOfferPreparation>>({});
  const [loading, setLoading] = useState(productId !== null);
  const [loadError, setLoadError] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [preparingAccountId, setPreparingAccountId] = useState<number | null>(null);
  const [ozonFooterAction, setOzonFooterAction] = useState<
    'save' | 'regenerate' | 'publish' | 'reconcile' | 'commerce' | null
  >(null);
  const [ozonPanel, setOzonPanel] = useState<'preparation' | 'pricing'>('preparation');
  const ozonPreparationRef = useRef<OzonOfferPreparationCardHandle>(null);
  const open = productId !== null;

  useEffect(() => {
    if (productId === null) return;
    let cancelled = false;
    void fetchPublicationWorkspace(productId)
      .then((snapshot) => {
        if (cancelled) return;
        setProduct(snapshot.product);
        setAccounts(snapshot.accounts);
        setImages(snapshot.images);
        setOzonPanel('preparation');
        setAvitoListings(snapshot.avitoListings);
        setOzonPreparations(snapshot.ozonPreparations);
        setLoadError(false);
      })
      .catch(() => {
        if (cancelled) return;
        setProduct(null);
        setAccounts([]);
        setImages([]);
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

  function selectAccount(accountId: number) {
    setOzonPanel('preparation');
    onSelectedAccountChange(accountId);
  }

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

  const selectedOzonPreparation = selectedAccount?.marketplace === 'ozon'
    ? ozonPreparations[selectedAccount.id] ?? null
    : null;

  async function saveOzonPreparation() {
    setOzonFooterAction('save');
    try {
      await ozonPreparationRef.current?.saveAttributes();
    } finally {
      setOzonFooterAction(null);
    }
  }

  function applyOzonMarketPrice(price: string) {
    if (ozonPreparationRef.current?.applyMarketPrice(price)) {
      setOzonPanel('preparation');
    }
  }

  async function regenerateProductForOzon() {
    if (!product) return;
    setOzonFooterAction('regenerate');
    try {
      await productApi.regenerate(product.id);
      toast.success(
        'Обогащение и новая генерация товара запущены. Ручные значения Ozon останутся без изменений.',
      );
    } catch (error: unknown) {
      const message = (
        error as { response?: { data?: { message?: string } } }
      ).response?.data?.message;
      toast.error(message ?? 'Не удалось запустить повторное обогащение товара.');
    } finally {
      setOzonFooterAction(null);
    }
  }

  async function publishProductToOzon() {
    if (!product || !selectedAccount || selectedAccount.marketplace !== 'ozon') return;
    if (typeof crypto === 'undefined' || typeof crypto.randomUUID !== 'function') {
      toast.error('Браузер не поддерживает безопасный идентификатор отправки.');
      return;
    }
    setOzonFooterAction('publish');
    try {
      const response = await productApi.publishOzonOffer(
        product.id,
        selectedAccount.id,
        crypto.randomUUID(),
      );
      const preparation = envelopeData<OzonOfferPreparation>(response.data);
      handleOzonPreparationChange(preparation);
      const state = preparation.publication.latest_operation?.state;
      if (state === 'reconciling') {
        toast.success('Ozon принял карточку. MAP сохранит и проверит результат задачи.');
      } else if (state === 'outcome_unknown') {
        toast.warning('Ответ Ozon не подтверждён. Не повторяйте отправку до сверки MAP.');
      } else {
        const message = preparation.publication.latest_operation?.errors[0]?.message;
        toast.error(message ?? 'Ozon не принял карточку. Проверьте сообщение в дровере.');
      }
    } catch (error: unknown) {
      const message = (
        error as { response?: { data?: { message?: string } } }
      ).response?.data?.message;
      toast.error(message ?? 'Не удалось отправить карточку Ozon. Повтор не выполнен автоматически.');
    } finally {
      setOzonFooterAction(null);
    }
  }

  async function reconcileProductInOzon() {
    if (!product || !selectedAccount || selectedAccount.marketplace !== 'ozon') return;
    setOzonFooterAction('reconcile');
    try {
      const response = await productApi.reconcileOzonOffer(product.id, selectedAccount.id);
      const preparation = envelopeData<OzonOfferPreparation>(response.data);
      handleOzonPreparationChange(preparation);
      const state = preparation.publication.latest_operation?.state;
      if (state === 'succeeded') {
        toast.success('Ozon подтвердил публикацию карточки.');
      } else if (state === 'failed') {
        toast.error('Ozon отклонил карточку. Исправьте указанные поля.');
      } else {
        toast.info('Статус обновлён. Ozon ещё обрабатывает карточку.');
      }
    } catch (error: unknown) {
      const message = (
        error as { response?: { data?: { message?: string } } }
      ).response?.data?.message;
      toast.error(message ?? 'Не удалось проверить статус карточки Ozon.');
    } finally {
      setOzonFooterAction(null);
    }
  }

  async function syncOzonCommerce() {
    if (!product || !selectedAccount || selectedAccount.marketplace !== 'ozon') return;
    if (typeof crypto === 'undefined' || typeof crypto.randomUUID !== 'function') {
      toast.error('Браузер не поддерживает безопасный идентификатор синхронизации.');
      return;
    }
    setOzonFooterAction('commerce');
    try {
      const response = await productApi.syncOzonCommerce(
        product.id,
        selectedAccount.id,
        crypto.randomUUID(),
      );
      const preparation = envelopeData<OzonOfferPreparation>(response.data);
      handleOzonPreparationChange(preparation);
      const operations = [preparation.commerce.price_operation, preparation.commerce.stock_operation];
      if (operations.some((operation) => operation?.state === 'outcome_unknown')) {
        toast.warning('Ozon мог принять изменение. MAP не будет повторять его вслепую.');
      } else if (operations.some((operation) => operation && operation.state !== 'succeeded')) {
        toast.error('Ozon отклонил часть данных. Подробности показаны в карточке.');
      } else {
        toast.success('Цена и остаток подтверждены Ozon.');
      }
    } catch (error: unknown) {
      const message = (error as { response?: { data?: { message?: string } } }).response?.data?.message;
      toast.error(message ?? 'Не удалось синхронизировать цену и остаток Ozon.');
    } finally {
      setOzonFooterAction(null);
    }
  }

  return (
    <Sheet open={open} onOpenChange={(nextOpen) => { if (!nextOpen) onClose(); }}>
      <SheetContent
        side="right"
        className="h-[100dvh] w-[100dvw] min-w-0 max-w-[100dvw] overflow-hidden p-0 sm:max-w-[100dvw] xl:w-[min(96vw,1440px)] xl:max-w-[min(96vw,1440px)]"
      >
        <SheetHeader className="sr-only">
          <SheetTitle>
            {product
              ? `Каналы публикации · ${product.article} · ${product.name}`
              : 'Каналы публикации'}
          </SheetTitle>
        </SheetHeader>
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
        ) : accounts.length === 0 ? (
          <div className="flex h-full items-center justify-center bg-muted/15 p-6">
            <div className="max-w-md rounded-lg border bg-background p-5 text-center text-sm">
              <p className="font-medium">Нет активных кабинетов маркетплейсов</p>
              <Button asChild className="mt-3" variant="outline">
                <Link href="/dashboard/settings#marketplaces">Открыть настройки</Link>
              </Button>
            </div>
          </div>
        ) : selectedView.kind === 'ozon' && selectedAccount ? (
          <div className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden">
            <div className="min-w-0 shrink-0 overflow-hidden border-b bg-background/95 px-4 pb-3 pt-[max(0.75rem,env(safe-area-inset-top))] backdrop-blur xl:hidden">
              <p className="truncate pr-10 font-mono text-xs text-muted-foreground">{product.article}</p>
              <p className="truncate pr-10 text-sm font-medium">{product.name}</p>
              <div className="mt-3 grid min-w-0 grid-cols-2 rounded-lg bg-muted p-1">
                <button
                  type="button"
                  onClick={() => setOzonPanel('preparation')}
                  className={`flex h-9 min-w-0 items-center justify-center gap-1 rounded-md text-xs font-medium transition-colors sm:gap-2 sm:text-sm ${ozonPanel === 'preparation' ? 'bg-background shadow-sm' : 'text-muted-foreground'}`}
                >
                  <FileText className="h-4 w-4" /> Карточка
                </button>
                <button
                  type="button"
                  onClick={() => setOzonPanel('pricing')}
                  className={`flex h-9 min-w-0 items-center justify-center gap-1 rounded-md text-xs font-medium transition-colors sm:gap-2 sm:text-sm ${ozonPanel === 'pricing' ? 'bg-background shadow-sm' : 'text-muted-foreground'}`}
                >
                  <BarChart3 className="h-4 w-4" /> Рынок
                </button>
              </div>
            </div>

            <div className="min-w-0 shrink-0 border-b bg-background px-4 py-3 sm:px-5">
              <MarketplaceChannelSwitcher
                accounts={accounts}
                selectedAccountId={selectedAccount.id}
                states={accountStates}
                onSelect={selectAccount}
              />
            </div>

            <div className="grid min-h-0 min-w-0 flex-1 grid-cols-[minmax(0,1fr)] overflow-hidden xl:grid-cols-[minmax(600px,1fr)_minmax(520px,560px)]">
              <section
                data-testid="ozon-market-audit-panel"
                className={`${ozonPanel === 'pricing' ? 'block' : 'hidden'} min-h-0 min-w-0 max-w-full overflow-x-hidden overflow-y-auto overscroll-contain border-r xl:block`}
              >
                <MarketPricingPanel
                  productId={product.id}
                  referencePrice={selectedOzonPreparation?.pricing?.final_price ?? product.price}
                  channelLabel="Ozon"
                  onApplyPrice={applyOzonMarketPrice}
                />
              </section>

              <section
                data-testid="ozon-listing-editor-panel"
                className={`${ozonPanel === 'preparation' ? 'block' : 'hidden'} min-h-0 min-w-0 max-w-full overflow-x-hidden overflow-y-auto overscroll-contain [scrollbar-gutter:stable] xl:block`}
              >
                <OzonListingEditorPanel
                  product={product}
                  account={selectedAccount}
                  preparation={selectedOzonPreparation}
                  images={images}
                  preparationRef={ozonPreparationRef}
                  footerAction={ozonFooterAction}
                  onPreparationChange={handleOzonPreparationChange}
                  onSave={() => void saveOzonPreparation()}
                  onRegenerate={() => void regenerateProductForOzon()}
                  onPublish={() => void publishProductToOzon()}
                  onReconcile={() => void reconcileProductInOzon()}
                  onSyncCommerce={() => void syncOzonCommerce()}
                />
              </section>
            </div>
          </div>
        ) : (
          <div className="h-full overflow-y-auto bg-muted/15 p-4 pb-10 sm:p-6">
            <SheetHeader className="pr-10 text-left">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-lg font-semibold text-foreground">Листинг товара</h2>
                <Badge variant="outline">{product.article}</Badge>
              </div>
              <p className="text-sm text-muted-foreground">{product.name}</p>
            </SheetHeader>
            <div className="mt-5 rounded-lg border bg-background p-4">
              <MarketplaceChannelSwitcher
                accounts={accounts}
                selectedAccountId={selectedAccount?.id ?? null}
                states={accountStates}
                onSelect={selectAccount}
              />
            </div>
            {selectedAccount && (
              <div className="mt-5 rounded-lg border bg-background p-4 sm:p-5">
                {selectedView.kind === 'avito_listing' ? (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Открываем данные Avito без промежуточного экрана…
                  </div>
                ) : (
                  <>
                    <p className="text-sm font-medium">Avito · {selectedAccount.name}</p>
                    <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
                      Для этого кабинета ещё нет черновика. MAP создаст его через текущую
                      проверенную логику Avito, без немедленной отправки.
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
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
