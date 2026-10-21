import { useEffect, useRef } from "react";
import { formatINR, formatNumber, formatPercent } from "../../lib/format";
import { Icon } from "../ui";
import {
  type DashboardTotals,
  isAboveReturnThreshold,
  RETURN_RATE_THRESHOLD_PERCENT,
  type StoreHealth,
} from "./metrics";

const LIMIT = `${RETURN_RATE_THRESHOLD_PERCENT.toFixed(1)}%`;

function SummaryTile({ label, value, note, alert = false }: { label: string; value: string; note: string; alert?: boolean }) {
  return (
    <div className="flex flex-col rounded-lg bg-surface-container-low p-3">
      <span className="text-[11px] font-semibold uppercase tracking-wider text-on-surface-variant">{label}</span>
      <span className={`mt-1 font-headline text-[1.75rem] font-bold leading-9 tracking-[-0.02em] ${alert ? "text-error" : "text-on-surface"}`}>
        {value}
      </span>
      <span className={`mt-1 text-[11px] font-semibold tracking-[0.03em] ${alert ? "text-error" : "text-tertiary"}`}>{note}</span>
    </div>
  );
}

function StatusPill({ store }: { store: StoreHealth }) {
  if (store.returnRatePercent === undefined) {
    return <span className="rounded bg-surface-container-high px-2 py-0.5 text-[11px] font-semibold text-on-surface-variant">No sales</span>;
  }
  return isAboveReturnThreshold(store) ? (
    <span className="rounded bg-error-container px-2 py-0.5 text-[11px] font-semibold text-on-error-container">Above {LIMIT}</span>
  ) : (
    <span className="rounded bg-tertiary-container/10 px-2 py-0.5 text-[11px] font-semibold text-tertiary">Healthy</span>
  );
}

export default function StoreHealthModal({
  stores,
  totals,
  onClose,
}: {
  stores: StoreHealth[];
  totals: DashboardTotals;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const flagged = stores.filter(isAboveReturnThreshold);
  const active = stores.filter((store) => store.unitsSold > 0).length;
  const networkHealthy = totals.returnRatePercent < RETURN_RATE_THRESHOLD_PERCENT;

  useEffect(() => closeRef.current?.focus(), []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-on-surface/40 p-3 backdrop-blur-sm"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="store-health-title"
        className="flex max-h-[90vh] w-full max-w-4xl flex-col overflow-hidden rounded-xl bg-surface-container-lowest shadow-xl"
      >
        <div className="flex items-center justify-between bg-surface-container-low p-4">
          <div className="flex flex-col">
            <div className="flex items-center gap-1">
              <Icon name="storefront" className="text-[20px] text-primary" />
              <h3 id="store-health-title" className="font-headline text-xl font-semibold tracking-[-0.015em] text-on-surface">
                All Stores Return Health
              </h3>
            </div>
            <p className="mt-0.5 text-xs text-on-surface-variant">Net revenue, returns and return rate for every store in this range</p>
          </div>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex rounded-lg p-1 text-on-surface-variant transition-colors hover:bg-surface-container hover:text-on-surface"
          >
            <Icon name="close" className="text-[20px]" />
          </button>
        </div>

        <div className="flex flex-col gap-3 overflow-y-auto p-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
            <SummaryTile
              label="Network Return Rate"
              value={formatPercent(totals.returnRatePercent)}
              note={networkHealthy ? `✓ Below the ${LIMIT} limit` : `At or above the ${LIMIT} limit`}
              alert={!networkHealthy}
            />
            <SummaryTile
              label="Store Locations"
              value={`${formatNumber(active)} / ${formatNumber(stores.length)} Active`}
              note="Active = at least one sale in this range"
            />
            <SummaryTile
              label="Flagged Locations"
              value={`${formatNumber(flagged.length)} Store${flagged.length === 1 ? "" : "s"}`}
              note={
                flagged.length === 0
                  ? `None at or above ${LIMIT}`
                  : `${flagged.slice(0, 2).map((store) => store.name).join(", ")}${flagged.length > 2 ? ` +${flagged.length - 2} more` : ""}`
              }
              alert={flagged.length > 0}
            />
          </div>

          <div className="w-full overflow-x-auto rounded-lg border border-outline-variant/40">
            <table className="w-full border-collapse text-left text-sm">
              <thead className="bg-surface-container-low text-[11px] font-semibold uppercase tracking-wider text-on-surface-variant">
                <tr>
                  <th className="p-2">Store Code &amp; Name</th>
                  <th className="p-2 text-right">Net Revenue</th>
                  <th className="p-2 text-right">Units Sold</th>
                  <th className="p-2 text-right">Units Returned</th>
                  <th className="p-2 text-center">Return Rate</th>
                  <th className="p-2 text-center">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-outline-variant/20 text-on-surface">
                {stores.map((store) => {
                  const flaggedRow = isAboveReturnThreshold(store);
                  return (
                    <tr key={store.code} className={flaggedRow ? "bg-error-container/20" : "hover:bg-surface-container-low/50"}>
                      <td className={`p-2 font-semibold ${flaggedRow ? "text-error" : ""}`}>
                        {store.code} • {store.name}
                      </td>
                      <td className="p-2 text-right font-mono text-xs">{formatINR(store.revenuePaise, 0)}</td>
                      <td className="p-2 text-right font-mono text-xs">{formatNumber(store.unitsSold)}</td>
                      <td className="p-2 text-right font-mono text-xs">{formatNumber(store.unitsReturned)}</td>
                      <td className={`p-2 text-center font-bold ${flaggedRow ? "text-error" : "text-tertiary"}`}>
                        {store.returnRatePercent === undefined ? "—" : formatPercent(store.returnRatePercent, 1)}
                      </td>
                      <td className="p-2 text-center">
                        <StatusPill store={store} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        <div className="flex items-center justify-between gap-3 bg-surface-container-low p-3">
          <span className="text-[11px] font-semibold tracking-[0.03em] text-on-surface-variant">
            Stores are flagged when returns reach {LIMIT} of units sold
          </span>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg bg-primary px-4 py-1.5 text-xs font-semibold tracking-[0.02em] text-on-primary transition-colors hover:bg-primary-container"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
