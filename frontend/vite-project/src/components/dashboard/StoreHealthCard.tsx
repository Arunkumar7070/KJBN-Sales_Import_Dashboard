import { useState } from "react";
import { formatCompactINR, formatNumber, formatPercent } from "../../lib/format";
import { EmptyState, Icon } from "../ui";
import { type DashboardTotals, isAboveReturnThreshold, type StoreHealth } from "./metrics";
import StoreHealthModal from "./StoreHealthModal";

const TOP_COUNT = 6;
// Bars fade from the top store down, as in the design.
const BAR_SHADES = [
  "bg-primary",
  "bg-primary-container",
  "bg-primary-container",
  "bg-primary-container/80",
  "bg-primary-container/70",
  "bg-primary-container/60",
];

function RatePill({ store }: { store: StoreHealth }) {
  if (store.returnRatePercent === undefined) {
    return <span className="rounded bg-surface-container-low px-2 py-0.5 text-[11px] font-semibold text-on-surface-variant">—</span>;
  }
  const flagged = isAboveReturnThreshold(store);
  return (
    <span
      title={`${formatNumber(store.unitsReturned)} of ${formatNumber(store.unitsSold)} units returned`}
      className={`rounded px-2 py-0.5 text-[11px] font-semibold tracking-[0.03em] ${
        flagged ? "bg-error-container text-on-error-container" : "bg-tertiary-container/10 text-tertiary"
      }`}
    >
      {formatPercent(store.returnRatePercent, 1)}
    </span>
  );
}

export default function StoreHealthCard({ stores, totals }: { stores: StoreHealth[]; totals: DashboardTotals }) {
  const [showAll, setShowAll] = useState(false);
  const top = stores.slice(0, TOP_COUNT);
  const topPaise = Math.max(0, ...top.map((store) => store.revenuePaise));

  return (
    <section className="flex flex-col justify-between gap-3 rounded-xl bg-surface-container-lowest p-4 shadow-sm xl:col-span-6">
      <div className="flex flex-col">
        <div className="flex items-center justify-between gap-2">
          <h2 className="font-headline text-lg font-semibold tracking-[-0.01em] text-on-surface">Store Comparison &amp; Return Health</h2>
          {stores.length > 0 && (
            <span className="shrink-0 rounded bg-surface-container-low px-2 py-0.5 text-[11px] font-semibold tracking-[0.03em] text-on-surface-variant">
              Top {top.length} of {formatNumber(stores.length)}
            </span>
          )}
        </div>
        <p className="mt-1 text-xs text-on-surface-variant">Net revenue by store, with each store's return rate against the 5% limit</p>
      </div>

      {stores.length === 0 ? (
        <EmptyState icon="storefront" title="No stores yet" hint="Import the store catalog on the Data Ingestion page." />
      ) : (
        <div className="flex flex-col gap-3 py-1">
          {top.map((store, index) => {
            const width = topPaise > 0 ? Math.max(0, Math.min(100, (store.revenuePaise / topPaise) * 100)) : 0;
            return (
              <div key={store.code} className="flex flex-col gap-1">
                <div className="flex items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-1 text-xs">
                    <span className="truncate font-semibold tracking-[0.02em] text-on-surface">
                      {store.code} • {store.name}
                    </span>
                    <span className={`shrink-0 font-mono ${store.revenuePaise < 0 ? "text-error" : "text-on-surface-variant"}`}>
                      {formatCompactINR(store.revenuePaise)}
                    </span>
                  </div>
                  <RatePill store={store} />
                </div>
                <div className="h-3 w-full overflow-hidden rounded-full bg-surface-container-low">
                  <div className={`h-full rounded-full ${BAR_SHADES[index]}`} style={{ width: `${width}%` }} />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {stores.length > 0 && (
        <div className="flex justify-end pt-1">
          <button
            type="button"
            onClick={() => setShowAll(true)}
            className="flex items-center gap-1 rounded bg-primary px-3 py-1 text-[11px] font-semibold tracking-[0.03em] text-on-primary shadow-sm transition-colors hover:bg-primary-container"
          >
            <Icon name="visibility" className="text-[14px]" />
            All Stores Return Health
          </button>
        </div>
      )}

      {showAll && <StoreHealthModal stores={stores} totals={totals} onClose={() => setShowAll(false)} />}
    </section>
  );
}
