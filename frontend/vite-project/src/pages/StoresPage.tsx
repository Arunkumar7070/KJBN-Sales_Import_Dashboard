import { useState } from "react";
import { Link } from "react-router-dom";
import { getDashboard, listStores } from "../api/client";
import type { DashboardResponse, Store } from "../api/types";
import { RETURN_RATE_THRESHOLD_PERCENT } from "../components/dashboard/metrics";
import { Card, EmptyState, ErrorPanel, Icon, Spinner, primaryButton } from "../components/ui";
import { formatINR, formatNumber } from "../lib/format";
import { useAsync } from "../lib/useAsync";

// Catalog uploads live on the Data Ingestion page, next to sales uploads.
const STORE_CATALOG_UPLOAD_PATH = "/ingestion?tab=stores";

interface StoresData {
  stores: Store[];
  /** All-time figures; undefined if analytics failed (the store list still shows). */
  analytics?: DashboardResponse;
}

async function loadStores(signal: AbortSignal): Promise<StoresData> {
  const [stores, analytics] = await Promise.all([
    listStores(signal),
    getDashboard(undefined, undefined, signal).catch(() => undefined),
  ]);
  return { stores, analytics };
}

function StoreCard({ store, data }: { store: Store; data: StoresData }) {
  const revenue = data.analytics?.store_revenue.find((row) => row.store_code === store.code);
  const returns = data.analytics?.store_return_rates.find((row) => row.store_code === store.code);
  const sold = returns !== undefined && returns.units_sold > 0;
  const flagged = sold && returns.return_rate_percent >= RETURN_RATE_THRESHOLD_PERCENT;
  return (
    <div className="flex flex-col justify-between gap-3 rounded-xl bg-surface-container-lowest p-4 shadow-sm transition-shadow hover:shadow-md">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="w-fit rounded bg-surface-container-high px-1.5 py-0.5 font-mono text-xs font-semibold text-primary">
            {store.code}
          </span>
          <h2 className="truncate font-headline text-lg font-semibold tracking-[-0.01em] text-on-surface" title={store.name}>
            {store.name}
          </h2>
        </div>
        <span className="flex shrink-0 items-center gap-1 rounded bg-surface-container-low px-2 py-0.5 text-[11px] font-semibold tracking-[0.03em] text-on-surface-variant">
          <Icon name="location_on" className="text-[14px]" />
          {store.city}
        </span>
      </div>
      <div className="flex flex-col gap-1 rounded-lg bg-surface-container-low p-3">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-on-surface-variant">Net Revenue (all time)</span>
        <span
          className={`font-headline text-2xl font-bold tracking-[-0.02em] ${
            revenue && revenue.revenue.paise < 0 ? "text-error" : "text-on-surface"
          }`}
        >
          {revenue ? formatINR(revenue.revenue.paise, 0) : "—"}
        </span>
        <div className="flex items-center justify-between gap-2 text-xs text-on-surface-variant">
          <span className="font-mono">{sold ? `${formatNumber(returns.units_sold)} units sold` : "No sales yet"}</span>
          {sold && (
            <span
              className={`rounded px-2 py-0.5 text-[11px] font-semibold tracking-[0.03em] ${
                flagged ? "bg-error-container text-on-error-container" : "bg-tertiary-container/10 text-tertiary"
              }`}
            >
              {returns.return_rate_percent.toFixed(1)}% returns
            </span>
          )}
        </div>
      </div>
      <Link
        to={`/transactions?store_code=${encodeURIComponent(store.code)}`}
        className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-surface-container-low px-3 py-2 text-xs font-semibold tracking-[0.02em] text-primary transition-colors hover:bg-primary-container hover:text-on-primary"
      >
        <Icon name="receipt_long" className="text-[16px]" />
        View Transactions
      </Link>
    </div>
  );
}

export default function StoresPage() {
  const [search, setSearch] = useState("");
  const { data, error, loading, reload } = useAsync(loadStores, []);

  const term = search.trim().toLowerCase();
  const visible = (data?.stores ?? []).filter(
    (store) =>
      !term ||
      store.code.toLowerCase().includes(term) ||
      store.name.toLowerCase().includes(term) ||
      store.city.toLowerCase().includes(term),
  );
  const totalPaise = data?.analytics?.store_revenue.reduce((sum, row) => sum + row.revenue.paise, 0);

  return (
    <div className="mx-auto flex w-full max-w-[1720px] flex-col gap-6">
      <Card className="flex flex-col justify-between gap-4 p-4 xl:flex-row xl:items-center">
        <div className="flex min-w-0 flex-col gap-1">
          {data && (
            <span className="w-fit rounded bg-surface-container-high px-1 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-primary">
              {formatNumber(data.stores.length)} Location{data.stores.length === 1 ? "" : "s"}
            </span>
          )}
          <h1 className="font-headline text-[1.75rem] font-bold leading-9 tracking-[-0.02em] text-on-surface">Store Management</h1>
          <p className="text-sm text-on-surface-variant">
            The store catalog with all-time revenue and return rates. Sales CSVs can only use store codes listed here.
          </p>
        </div>
        <div className="flex w-full flex-wrap items-center gap-3 xl:w-auto">
          <div className="relative flex-1 sm:min-w-72">
            <Icon name="search" className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[16px] text-on-surface-variant" />
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Filter by code, name, or city…"
              aria-label="Filter stores by code, name, or city"
              className="h-9 w-full rounded-xl bg-surface-container-low pl-9 pr-3 text-xs text-on-surface transition-all placeholder:text-on-surface-variant/70 focus:bg-surface-container-lowest focus:outline-none focus:ring-2 focus:ring-primary-container"
            />
          </div>
          <Link to={STORE_CATALOG_UPLOAD_PATH} className={primaryButton}>
            <Icon name="add_business" className="text-[16px]" />
            Import Store Catalog
          </Link>
        </div>
      </Card>

      {error && <ErrorPanel error={error} onRetry={reload} />}
      {!data && loading && <Spinner label="Loading stores…" />}
      {data && !data.analytics && (
        <p role="status" className="rounded-lg bg-amber-100 px-3 py-2 text-xs text-amber-900">
          Revenue figures are unavailable right now; showing the store list only.
        </p>
      )}

      {data && data.stores.length === 0 && (
        <Card className="p-6">
          <EmptyState
            icon="storefront"
            title="No stores in the catalog yet"
            hint="Sales rows are rejected until their store code exists. Import a CSV with columns code, name, city to get started."
            action={
              <Link to={STORE_CATALOG_UPLOAD_PATH} className={`${primaryButton} mt-2`}>
                <Icon name="upload_file" className="text-[16px]" />
                Import Store Catalog
              </Link>
            }
          />
        </Card>
      )}

      {data && data.stores.length > 0 && (
        <>
          {visible.length === 0 ? (
            <Card className="p-6">
              <EmptyState icon="search_off" title={`No stores match “${search}”`} />
            </Card>
          ) : (
            <div className={`grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3 ${loading ? "opacity-60" : ""}`}>
              {visible.map((store) => (
                <StoreCard key={store.code} store={store} data={data} />
              ))}
            </div>
          )}
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-surface-container-low px-4 py-2">
            <span className="text-[11px] font-semibold tracking-[0.03em] text-on-surface-variant">
              Showing {formatNumber(visible.length)} of {formatNumber(data.stores.length)} stores
            </span>
            <span className="flex items-center gap-2 text-[11px] font-semibold tracking-[0.03em] text-on-surface-variant">
              Consolidated net revenue (all time):
              <span className="font-mono text-xs font-bold text-primary">
                {totalPaise !== undefined ? formatINR(totalPaise) : "—"}
              </span>
            </span>
          </div>
        </>
      )}
    </div>
  );
}
