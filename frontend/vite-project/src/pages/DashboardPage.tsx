import { useState } from "react";
import { getDashboard } from "../api/client";
import type { DashboardResponse } from "../api/types";
import KpiCards from "../components/dashboard/KpiCards";
import { dashboardTotals, storeHealth } from "../components/dashboard/metrics";
import PaymentSplitCard from "../components/dashboard/PaymentSplitCard";
import RevenueTrendCard from "../components/dashboard/RevenueTrendCard";
import StoreHealthCard from "../components/dashboard/StoreHealthCard";
import TopProductsCard from "../components/dashboard/TopProductsCard";
import { ErrorPanel, Icon, Spinner } from "../components/ui";
import { addDays, daysBetween, istToday, startOfIstMonth } from "../lib/format";
import { useAsync } from "../lib/useAsync";

interface DateRange {
  from?: string; // undefined = all time
  to?: string;
}

type Preset = "last7" | "month" | "all";

const PRESETS: [Preset, string][] = [
  ["last7", "Last 7 Days"],
  ["month", "This Month"],
  ["all", "All Time"],
];

function presetRange(preset: Preset): DateRange {
  const today = istToday();
  if (preset === "last7") return { from: addDays(today, -6), to: today };
  if (preset === "month") return { from: startOfIstMonth(), to: today };
  return {};
}

/** The equally long range just before `range` (undefined for open ranges). */
function previousRange(range: DateRange): DateRange | undefined {
  if (!range.from || !range.to) return undefined;
  const length = daysBetween(range.from, range.to);
  return { from: addDays(range.from, -length), to: addDays(range.from, -1) };
}

interface DashboardData {
  current: DashboardResponse;
  previous?: DashboardResponse;
}

async function loadDashboard(range: DateRange, signal: AbortSignal): Promise<DashboardData> {
  const prior = previousRange(range);
  const [current, previous] = await Promise.all([
    getDashboard(range.from, range.to, signal),
    // The comparison is a bonus: if it fails, show the dashboard without deltas.
    prior ? getDashboard(prior.from, prior.to, signal).catch(() => undefined) : Promise.resolve(undefined),
  ]);
  return { current, previous };
}

const dateInput = "w-[7.5rem] bg-transparent text-center font-mono text-xs text-on-surface focus:text-primary focus:outline-none";

export default function DashboardPage() {
  const [preset, setPreset] = useState<Preset | null>("month");
  const [applied, setApplied] = useState<DateRange>(() => presetRange("month"));
  const [draftFrom, setDraftFrom] = useState(applied.from ?? "");
  const [draftTo, setDraftTo] = useState(applied.to ?? "");
  const [rangeError, setRangeError] = useState<string>();

  const { data, error, loading, reload } = useAsync(
    (signal) => loadDashboard(applied, signal),
    [applied.from, applied.to],
  );

  function applyPreset(next: Preset) {
    const range = presetRange(next);
    setPreset(next);
    setDraftFrom(range.from ?? "");
    setDraftTo(range.to ?? "");
    setRangeError(undefined);
    setApplied(range);
  }

  function applyDraft() {
    if (draftFrom && draftTo && draftFrom > draftTo) {
      setRangeError("Start date must be on or before end date.");
      return;
    }
    setRangeError(undefined);
    setPreset(null);
    setApplied({ from: draftFrom || undefined, to: draftTo || undefined });
  }

  const totals = data && dashboardTotals(data.current);
  // Only compare against a previous period that actually had sales.
  const previousTotals =
    data?.previous && dashboardTotals(data.previous).unitsSold > 0 ? dashboardTotals(data.previous) : undefined;
  const scopeLabel = PRESETS.find(([key]) => key === preset)?.[1] ?? "Custom Range";

  return (
    <div className="mx-auto flex w-full max-w-[1720px] flex-col gap-6">
      <section className="flex flex-col justify-between gap-4 rounded-xl bg-surface-container-lowest p-4 shadow-sm xl:flex-row xl:items-center">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="w-fit rounded bg-surface-container-high px-1 py-0.5 text-[11px] font-semibold uppercase tracking-wider text-primary">
            {scopeLabel} · IST
          </span>
          <h1 className="font-headline text-[1.75rem] font-bold leading-9 tracking-[-0.02em] text-on-surface">
            Executive Analytics Dashboard
          </h1>
          <p className="text-sm text-on-surface-variant">
            Multi-store revenue performance, unit velocity, and return risk across the store network
          </p>
        </div>
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            applyDraft();
          }}
        >
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-1 rounded-xl bg-surface-container-low px-3 py-2">
              <Icon name="calendar_month" className="text-[16px] text-primary" />
              <input
                type="date"
                aria-label="From date (IST)"
                value={draftFrom}
                onChange={(event) => setDraftFrom(event.target.value)}
                className={dateInput}
              />
              <span className="text-[11px] text-outline-variant">→</span>
              <input
                type="date"
                aria-label="To date (IST)"
                value={draftTo}
                onChange={(event) => setDraftTo(event.target.value)}
                className={dateInput}
              />
            </div>
            <button
              type="submit"
              disabled={loading}
              className="flex items-center gap-1 rounded-xl bg-primary px-4 py-2 text-xs font-semibold tracking-[0.02em] text-on-primary shadow-sm transition-colors hover:bg-primary-container disabled:opacity-60"
            >
              <Icon name={loading ? "progress_activity" : "filter_alt"} className={`text-[16px] ${loading ? "animate-spin" : ""}`} />
              Apply
            </button>
          </div>
          <div className="flex w-fit items-center rounded-lg bg-surface-container-low p-1" role="group" aria-label="Quick ranges">
            {PRESETS.map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => applyPreset(key)}
                aria-pressed={preset === key}
                className={`rounded px-3 py-1 text-xs font-semibold tracking-[0.02em] transition-all ${
                  preset === key ? "bg-surface-container-lowest text-primary shadow-sm" : "text-on-surface-variant hover:text-on-surface"
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          {rangeError && (
            <p role="alert" className="text-xs text-error">
              {rangeError}
            </p>
          )}
        </form>
      </section>

      {error && <ErrorPanel error={error} onRetry={reload} />}
      {!data && loading && <Spinner label="Loading dashboard…" />}

      {data && totals && (
        <div className={`flex flex-col gap-6 transition-opacity ${loading ? "opacity-60" : ""}`} aria-busy={loading}>
          <KpiCards totals={totals} previous={previousTotals} />
          <RevenueTrendCard daily={data.current.daily_revenue} />
          <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
            <StoreHealthCard stores={storeHealth(data.current)} totals={totals} />
            <PaymentSplitCard split={data.current.payment_split} />
          </div>
          <TopProductsCard products={data.current.top_products} netUnits={totals.unitsSold - totals.unitsReturned} />
        </div>
      )}
    </div>
  );
}
