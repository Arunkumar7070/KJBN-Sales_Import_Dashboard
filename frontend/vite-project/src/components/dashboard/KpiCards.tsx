import type { ReactNode } from "react";
import { formatINR, formatNumber, formatPercent } from "../../lib/format";
import { Icon } from "../ui";
import { type DashboardTotals, percentChange, RETURN_RATE_THRESHOLD_PERCENT } from "./metrics";

function Delta({
  change,
  suffix,
  higherIsBetter = true,
}: {
  change: number | undefined;
  suffix: string;
  higherIsBetter?: boolean;
}) {
  if (change === undefined) return null;
  // Compare the shown (rounded) value, so a tiny change never reads as "-0.0".
  const shown = Math.round(change * 10) / 10;
  const good = higherIsBetter ? shown > 0 : shown < 0;
  const tone =
    shown === 0
      ? "bg-surface-container-high text-on-surface-variant"
      : good
        ? "bg-tertiary-container/10 text-tertiary"
        : "bg-error-container text-on-error-container";
  return (
    <div className="flex items-center gap-1.5">
      <span className={`inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 font-mono text-[11px] font-semibold ${tone}`}>
        {shown !== 0 && <Icon name={shown > 0 ? "arrow_upward" : "arrow_downward"} className="text-[12px]" />}
        {shown > 0 ? "+" : ""}
        {Math.abs(shown) === 0 ? "0.0" : shown.toFixed(1)}
        {suffix}
      </span>
      <span className="text-[11px] text-on-surface-variant">vs previous period</span>
    </div>
  );
}

function KpiCard({
  label,
  icon,
  iconClass,
  value,
  unit,
  delta,
}: {
  label: string;
  icon: string;
  iconClass: string;
  value: string;
  unit?: string;
  delta: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-xl bg-surface-container-lowest p-4 shadow-sm transition-shadow hover:shadow-md">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-on-surface-variant">{label}</span>
          <div className="mt-1 flex items-baseline gap-1">
            <span className="font-headline text-3xl font-bold tracking-[-0.025em] text-on-surface 2xl:text-4xl">{value}</span>
            {unit && <span className="text-sm text-on-surface-variant">{unit}</span>}
          </div>
        </div>
        <div className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-surface-container-low ${iconClass}`}>
          <Icon name={icon} className="text-[20px]" />
        </div>
      </div>
      {delta}
    </div>
  );
}

export default function KpiCards({
  totals,
  previous,
}: {
  totals: DashboardTotals;
  previous: DashboardTotals | undefined;
}) {
  const healthy = totals.returnRatePercent < RETURN_RATE_THRESHOLD_PERCENT;
  return (
    <section className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <KpiCard
        label="Total Net Revenue (₹)"
        icon="payments"
        iconClass="text-primary"
        value={formatINR(totals.netRevenuePaise, 0)}
        delta={<Delta change={previous && percentChange(totals.netRevenuePaise, previous.netRevenuePaise)} suffix="%" />}
      />
      <KpiCard
        label="Total Units Sold"
        icon="shopping_cart"
        iconClass="text-primary"
        value={formatNumber(totals.unitsSold)}
        unit="units"
        delta={<Delta change={previous && percentChange(totals.unitsSold, previous.unitsSold)} suffix="%" />}
      />
      <KpiCard
        label="Total Returns (Units)"
        icon="assignment_return"
        iconClass="text-secondary"
        value={formatNumber(totals.unitsReturned)}
        unit="units"
        delta={
          <Delta
            change={previous && percentChange(totals.unitsReturned, previous.unitsReturned)}
            suffix="%"
            higherIsBetter={false}
          />
        }
      />
      <KpiCard
        label="Overall Return Rate (%)"
        icon={healthy ? "verified" : "warning"}
        iconClass={healthy ? "text-tertiary" : "text-error"}
        value={formatPercent(totals.returnRatePercent)}
        delta={
          <Delta
            change={previous && totals.returnRatePercent - previous.returnRatePercent}
            suffix=" pp"
            higherIsBetter={false}
          />
        }
      />
    </section>
  );
}
