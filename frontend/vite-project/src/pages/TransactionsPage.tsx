import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { listStores, listTransactions, type TransactionFilters } from "../api/client";
import type { Store } from "../api/types";
import LedgerTable from "../components/transactions/LedgerTable";
import { EmptyState, ErrorPanel, Icon, Spinner } from "../components/ui";
import { formatNumber } from "../lib/format";
import { useAsync } from "../lib/useAsync";

const PAGE_SIZES = [10, 20, 50, 100];
const DEFAULT_PAGE_SIZE = 20;

const labelClass = "whitespace-nowrap text-[11px] font-semibold uppercase tracking-[0.03em] text-on-surface-variant";
const fieldClass =
  "h-9 rounded-lg bg-surface-container-low text-on-surface transition-all placeholder:text-on-surface-variant/60 focus:bg-surface-container-lowest focus:outline-none focus:ring-2 focus:ring-primary-container";
const footerButton =
  "flex h-8 items-center gap-1 rounded-lg bg-surface-container-lowest px-3 text-[11px] font-semibold tracking-[0.03em] text-on-surface shadow-sm transition-colors hover:bg-surface-container disabled:cursor-not-allowed disabled:text-outline-variant disabled:hover:bg-surface-container-lowest";

/** Applied filters live in the URL, so views can be shared and the Stores page can link here. */
function readQuery(params: URLSearchParams) {
  const page = Math.max(1, Number(params.get("page")) || 1);
  const limitParam = Number(params.get("limit"));
  const limit = PAGE_SIZES.includes(limitParam) ? limitParam : DEFAULT_PAGE_SIZE;
  const filters: TransactionFilters = {
    storeCode: params.get("store_code") || undefined,
    sku: params.get("sku") || undefined,
    dateFrom: params.get("date_from") || undefined,
    dateTo: params.get("date_to") || undefined,
    returnsOnly: params.get("returns_only") === "true",
  };
  return { filters, page, limit };
}

function writeQuery(filters: TransactionFilters, page: number, limit: number): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.storeCode) params.set("store_code", filters.storeCode);
  if (filters.sku?.trim()) params.set("sku", filters.sku.trim());
  if (filters.dateFrom) params.set("date_from", filters.dateFrom);
  if (filters.dateTo) params.set("date_to", filters.dateTo);
  if (filters.returnsOnly) params.set("returns_only", "true");
  if (page > 1) params.set("page", String(page));
  if (limit !== DEFAULT_PAGE_SIZE) params.set("limit", String(limit));
  return params;
}

/** "All Stores (ST01–ST06)" once the store list is known (it arrives sorted by code). */
function allStoresLabel(stores: Store[] | undefined, failed: boolean): string {
  if (failed) return "All Stores (store list unavailable)";
  if (!stores?.length) return "All Stores";
  return `All Stores (${stores[0].code}–${stores[stores.length - 1].code})`;
}

export default function TransactionsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { filters, page, limit } = readQuery(searchParams);
  const [draft, setDraft] = useState<TransactionFilters>(filters);
  const [rangeError, setRangeError] = useState<string>();
  const queryKey = searchParams.toString();

  // Keep the inputs in step with the URL (back/forward, links from other pages).
  useEffect(() => {
    setDraft(readQuery(new URLSearchParams(queryKey)).filters);
  }, [queryKey]);

  const stores = useAsync((signal) => listStores(signal), []);
  const ledger = useAsync(
    (signal) => listTransactions(filters, page, limit, signal),
    [queryKey],
  );

  function apply(next: TransactionFilters) {
    if (next.dateFrom && next.dateTo && next.dateFrom > next.dateTo) {
      setRangeError("Start date must be on or before end date.");
      return;
    }
    setRangeError(undefined);
    setSearchParams(writeQuery(next, 1, limit));
  }

  function reset() {
    setDraft({});
    setRangeError(undefined);
    setSearchParams(new URLSearchParams());
  }

  const items = ledger.data?.items ?? [];
  const total = ledger.data?.total ?? 0;
  const pages = Math.max(1, ledger.data?.pages ?? 1);
  const firstRow = (page - 1) * limit + 1;
  const lastRow = firstRow + items.length - 1;
  const hasFilters = [filters.storeCode, filters.sku, filters.dateFrom, filters.dateTo, filters.returnsOnly].some(Boolean);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h1 className="font-headline text-[1.75rem] font-bold leading-9 tracking-[-0.02em] text-on-surface">
          Transaction Explorer
        </h1>
        <p className="text-sm text-on-surface-variant">Search and review POS transactions and returns across outlets.</p>
      </div>

      <form
        className="flex flex-col gap-2 rounded-xl bg-surface-container-lowest p-3 shadow-sm"
        onSubmit={(event) => {
          event.preventDefault();
          apply(draft);
        }}
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-1 flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <label className={labelClass} htmlFor="sku-filter">
                SKU
              </label>
              <span className="relative flex items-center">
                <Icon name="search" className="pointer-events-none absolute left-2.5 text-[16px] text-on-surface-variant" />
                <input
                  id="sku-filter"
                  value={draft.sku ?? ""}
                  onChange={(event) => setDraft({ ...draft, sku: event.target.value })}
                  placeholder="Exact SKU…"
                  className={`${fieldClass} w-44 pl-8 pr-3 font-mono text-xs`}
                />
              </span>
            </div>
            <div className="flex items-center gap-2">
              <label className={labelClass} htmlFor="store-filter">
                Store
              </label>
              <span className="relative flex items-center">
                <select
                  id="store-filter"
                  value={draft.storeCode ?? ""}
                  onChange={(event) => setDraft({ ...draft, storeCode: event.target.value || undefined })}
                  className={`${fieldClass} w-56 cursor-pointer appearance-none truncate pl-3 pr-8 text-xs`}
                  disabled={stores.loading && !stores.data}
                >
                  <option value="">{allStoresLabel(stores.data, Boolean(stores.error))}</option>
                  {stores.data?.map((store) => (
                    <option key={store.code} value={store.code}>
                      {store.code} - {store.name}
                    </option>
                  ))}
                </select>
                <Icon name="unfold_more" className="pointer-events-none absolute right-2 text-[16px] text-on-surface-variant" />
              </span>
            </div>
            <div className="flex items-center gap-2">
              <span className={labelClass} id="date-range-label">
                Date Range
              </span>
              <div className="flex items-center gap-1.5" role="group" aria-labelledby="date-range-label">
                <input
                  type="date"
                  aria-label="From date (IST)"
                  value={draft.dateFrom ?? ""}
                  onChange={(event) => setDraft({ ...draft, dateFrom: event.target.value || undefined })}
                  className={`${fieldClass} px-2 font-mono text-xs`}
                />
                <span className="text-xs text-on-surface-variant">to</span>
                <input
                  type="date"
                  aria-label="To date (IST)"
                  value={draft.dateTo ?? ""}
                  onChange={(event) => setDraft({ ...draft, dateTo: event.target.value || undefined })}
                  className={`${fieldClass} px-2 font-mono text-xs`}
                />
              </div>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={Boolean(draft.returnsOnly)}
              onClick={() => setDraft({ ...draft, returnsOnly: !draft.returnsOnly })}
              className="flex items-center gap-2 pl-2"
            >
              <span
                className={`relative h-5 w-9 shrink-0 rounded-full transition-colors ${
                  draft.returnsOnly ? "bg-error" : "bg-outline-variant"
                }`}
              >
                <span
                  className={`absolute left-0.5 top-0.5 h-4 w-4 rounded-full bg-white transition-transform ${
                    draft.returnsOnly ? "translate-x-4" : ""
                  }`}
                />
              </span>
              <span className="whitespace-nowrap text-[11px] font-semibold tracking-[0.03em] text-on-surface">
                Returns Only (quantity &lt; 0)
              </span>
            </button>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={reset}
              className="h-9 rounded-lg bg-surface-container-low px-3 text-xs font-semibold tracking-[0.02em] text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface"
            >
              Reset
            </button>
            <button
              type="submit"
              className="flex h-9 items-center gap-1.5 rounded-lg bg-primary-container px-4 text-xs font-semibold tracking-[0.02em] text-on-primary shadow-sm transition-colors hover:bg-primary"
            >
              <Icon name="filter_alt" className="text-[16px]" />
              Apply
            </button>
          </div>
        </div>
        {rangeError && (
          <p role="alert" className="text-xs text-error">
            {rangeError}
          </p>
        )}
      </form>

      <section className="flex flex-col overflow-hidden rounded-xl bg-surface-container-lowest shadow-sm">
        {ledger.error && (
          <div className="p-4">
            <ErrorPanel error={ledger.error} onRetry={ledger.reload} />
          </div>
        )}
        {!ledger.data && ledger.loading && <Spinner label="Loading transactions…" />}
        {ledger.data && items.length === 0 && (
          <EmptyState
            icon="receipt_long"
            title={total === 0 ? "No transactions match these filters" : "This page is empty"}
            hint={
              total === 0
                ? hasFilters
                  ? "Try widening the date range or clearing filters."
                  : "Upload sales CSVs on the CSV Ingestion page to populate the ledger."
                : "Go back to an earlier page."
            }
            action={
              hasFilters ? (
                <button
                  type="button"
                  onClick={reset}
                  className="mt-2 h-8 rounded-lg bg-surface-container-low px-3 text-xs font-semibold text-on-surface transition-colors hover:bg-surface-container"
                >
                  Clear filters
                </button>
              ) : undefined
            }
          />
        )}
        {items.length > 0 && (
          <div className={`transition-opacity ${ledger.loading ? "opacity-60" : ""}`} aria-busy={ledger.loading}>
            <LedgerTable transactions={items} />
          </div>
        )}

        {ledger.data && total > 0 && (
          <div className="flex flex-col justify-between gap-3 border-t border-surface-container-high bg-surface-container-low px-4 py-3 lg:flex-row lg:items-center">
            <div className="flex flex-wrap items-center gap-3 text-xs text-on-surface-variant">
              <span>
                {items.length > 0 ? (
                  <>
                    Showing {formatNumber(firstRow)}–{formatNumber(lastRow)} of{" "}
                  </>
                ) : (
                  "No rows on this page · "
                )}
                <span className="font-mono font-semibold text-on-surface">{formatNumber(total)}</span> transactions
              </span>
              <label className="flex items-center gap-1.5">
                Rows
                <select
                  value={limit}
                  onChange={(event) => setSearchParams(writeQuery(filters, 1, Number(event.target.value)))}
                  className="h-7 cursor-pointer rounded-lg bg-surface-container-lowest px-1.5 font-mono text-xs text-on-surface shadow-sm focus:outline-none focus:ring-2 focus:ring-primary-container"
                >
                  {PAGE_SIZES.map((size) => (
                    <option key={size} value={size}>
                      {size}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <nav className="flex items-center gap-2" aria-label="Pagination">
              <button
                type="button"
                className={footerButton}
                disabled={page <= 1}
                onClick={() => setSearchParams(writeQuery(filters, Math.min(page - 1, pages), limit))}
              >
                <Icon name="chevron_left" className="text-[14px]" />
                Previous
              </button>
              <span className="px-1 font-mono text-xs text-on-surface">
                Page {formatNumber(page)} of {formatNumber(pages)}
              </span>
              <button
                type="button"
                className={footerButton}
                disabled={page >= pages}
                onClick={() => setSearchParams(writeQuery(filters, page + 1, limit))}
              >
                Next
                <Icon name="chevron_right" className="text-[14px]" />
              </button>
            </nav>
          </div>
        )}
      </section>
    </div>
  );
}
