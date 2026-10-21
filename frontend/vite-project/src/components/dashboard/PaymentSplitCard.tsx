import { Cell, Pie, PieChart, ResponsiveContainer } from "recharts";
import type { DashboardResponse, PaymentMode } from "../../api/types";
import { formatCompactINR, formatINR, formatNumber, paiseToRupeeNumber } from "../../lib/format";

// Recharts needs literal colours; these are the Vantage Ledger tokens.
const MODE_STYLE: Record<PaymentMode, { label: string; color: string; dot: string }> = {
  UPI: { label: "UPI Payments", color: "#004ac6", dot: "bg-primary" },
  CARD: { label: "Credit/Debit Cards", color: "#2563eb", dot: "bg-primary-container" },
  CASH: { label: "Cash", color: "#007d55", dot: "bg-tertiary-container" },
};
const MODE_ORDER: PaymentMode[] = ["UPI", "CARD", "CASH"];
const EMPTY_TRACK = "#eff4ff";

type Split = DashboardResponse["payment_split"][number];

export default function PaymentSplitCard({ split }: { split: DashboardResponse["payment_split"] }) {
  const modes = MODE_ORDER.map((mode) => split.find((entry) => entry.payment_mode === mode)).filter(
    (entry): entry is Split => entry !== undefined,
  );
  const totalPaise = modes.reduce((sum, entry) => sum + entry.revenue.paise, 0);
  const slices =
    totalPaise > 0
      ? modes.map((entry) => ({ mode: entry.payment_mode, value: paiseToRupeeNumber(entry.revenue.paise) }))
      : [{ mode: "NONE", value: 1 }];

  return (
    <section className="flex flex-col gap-3 rounded-xl bg-surface-container-lowest p-4 shadow-sm xl:col-span-6">
      <div className="flex flex-col">
        <h2 className="font-headline text-lg font-semibold tracking-[-0.01em] text-on-surface">Payment Mode Settlement Share</h2>
        <p className="mt-1 text-xs text-on-surface-variant">Sales revenue split by payment mode (returns excluded)</p>
      </div>
      <div className="flex flex-1 flex-col items-center justify-around gap-4 py-2 sm:flex-row">
        <div className="relative h-48 w-48 shrink-0">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={slices}
                dataKey="value"
                nameKey="mode"
                innerRadius="62%"
                outerRadius="100%"
                startAngle={90}
                endAngle={-270}
                stroke="none"
                isAnimationActive={false}
              >
                {slices.map((slice) => (
                  <Cell key={slice.mode} fill={slice.mode === "NONE" ? EMPTY_TRACK : MODE_STYLE[slice.mode as PaymentMode].color} />
                ))}
              </Pie>
            </PieChart>
          </ResponsiveContainer>
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
            {totalPaise > 0 ? (
              <>
                <span className="font-mono text-[11px] uppercase tracking-wide text-on-surface-variant">Gross Sales</span>
                <span className="font-headline text-lg font-bold leading-tight text-on-surface">{formatCompactINR(totalPaise)}</span>
                <span className="text-[11px] font-semibold tracking-[0.03em] text-tertiary">Settled</span>
              </>
            ) : (
              <span className="px-8 text-xs text-on-surface-variant">No sales in this range</span>
            )}
          </div>
        </div>
        <div className="flex w-full min-w-0 max-w-xs flex-col gap-2">
          {modes.map((entry) => {
            const style = MODE_STYLE[entry.payment_mode];
            return (
              <div
                key={entry.payment_mode}
                className="flex items-center justify-between gap-3 rounded-lg p-1 transition-colors hover:bg-surface-container-low"
              >
                <div className="flex items-center gap-1.5">
                  <span className={`h-3 w-3 shrink-0 rounded-full ${style.dot}`} />
                  <div className="flex flex-col">
                    <span className="text-xs font-semibold tracking-[0.02em] text-on-surface">{style.label}</span>
                    <span className="font-mono text-xs text-on-surface-variant">
                      {formatNumber(entry.transaction_count)} txns • {entry.share_percent.toFixed(1)}%
                    </span>
                  </div>
                </div>
                <span className="shrink-0 font-mono text-xs font-bold text-on-surface">{formatINR(entry.revenue.paise, 0)}</span>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}
