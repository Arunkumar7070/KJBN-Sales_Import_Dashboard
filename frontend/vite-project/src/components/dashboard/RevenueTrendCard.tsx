import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceArea,
  ReferenceDot,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { DashboardResponse } from "../../api/types";
import {
  formatCompactINR,
  formatDayLong,
  formatDayShort,
  formatINR,
  paiseToRupeeNumber,
} from "../../lib/format";
import { EmptyState } from "../ui";

const MOVING_AVERAGE_DAYS = 7;

// Recharts needs literal colours; these are the Vantage Ledger tokens.
const COLOR = {
  revenue: "#004ac6",
  average: "#565e74",
  weekend: "#2563eb",
  grid: "#c3c6d7",
  tick: "#434655",
  dotStroke: "#ffffff",
};

interface Point {
  day: string;
  paise: number;
  averagePaise: number;
  revenue: number; // rupees, for the chart's scale only
  average: number;
}

function withMovingAverage(daily: DashboardResponse["daily_revenue"]): Point[] {
  return daily.map((entry, index) => {
    const window = daily.slice(Math.max(0, index - MOVING_AVERAGE_DAYS + 1), index + 1);
    const averagePaise = Math.round(window.reduce((sum, item) => sum + item.revenue.paise, 0) / window.length);
    return {
      day: entry.day,
      paise: entry.revenue.paise,
      averagePaise,
      revenue: paiseToRupeeNumber(entry.revenue.paise),
      average: paiseToRupeeNumber(averagePaise),
    };
  });
}

/** [Saturday, Sunday-or-Saturday] pairs to shade; days are IST calendar dates (YYYY-MM-DD). */
function weekends(points: Point[]): [string, string][] {
  const isWeekday = (day: string, weekday: number) => new Date(`${day}T00:00:00Z`).getUTCDay() === weekday;
  return points
    .map((point, index) => ({ point, index }))
    .filter(({ point }) => isWeekday(point.day, 6))
    .map(({ point, index }) => {
      const next = points[index + 1];
      return [point.day, next && isWeekday(next.day, 0) ? next.day : point.day];
    });
}

function TrendTooltip({ active, payload }: { active?: boolean; payload?: { payload: Point }[] }) {
  if (!active || !payload?.length) return null;
  const point = payload[0].payload;
  return (
    <div className="flex flex-col gap-0.5 rounded bg-inverse-surface px-2.5 py-1.5 text-inverse-on-surface shadow-lg">
      <span className="font-mono text-[11px] uppercase opacity-80">{formatDayLong(point.day)} (IST)</span>
      <span className="font-mono text-sm font-bold">{formatINR(point.paise)}</span>
      <span className="font-mono text-[11px] opacity-80">7-day avg {formatINR(point.averagePaise, 0)}</span>
    </div>
  );
}

export default function RevenueTrendCard({ daily }: { daily: DashboardResponse["daily_revenue"] }) {
  const points = withMovingAverage(daily);
  const peak = points.reduce<Point | undefined>((best, point) => (!best || point.paise > best.paise ? point : best), undefined);
  const range = points.length ? `${formatDayLong(points[0].day)} – ${formatDayLong(points[points.length - 1].day)}` : "";
  const hasRevenue = points.some((point) => point.paise !== 0);

  return (
    <section className="flex flex-col gap-3 rounded-xl bg-surface-container-lowest p-4 shadow-sm">
      <div className="flex flex-col justify-between gap-2 sm:flex-row sm:items-center">
        <div className="flex flex-col">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-headline text-xl font-semibold tracking-[-0.015em] text-on-surface">Daily Net Revenue Trend</h2>
            {range && (
              <span className="rounded bg-surface-container-low px-2 py-0.5 text-[11px] font-semibold tracking-[0.03em] text-primary">
                {range}
              </span>
            )}
          </div>
          <p className="text-xs text-on-surface-variant">
            Net revenue per IST day (returns subtracted) with a 7-day moving average
            {peak && hasRevenue && `; peak ${formatCompactINR(peak.paise)} on ${formatDayShort(peak.day)}`}. Weekends are
            shaded.
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-4 text-[11px] font-semibold tracking-[0.03em] text-on-surface-variant">
          <span className="flex items-center gap-1">
            <span className="h-3 w-3 rounded-sm bg-primary" /> Daily Net Revenue (₹)
          </span>
          <span className="flex items-center gap-1">
            <span className="h-0 w-4 border-t-2 border-dashed border-secondary" /> 7-Day Moving Avg
          </span>
        </div>
      </div>
      {!hasRevenue ? (
        <EmptyState icon="show_chart" title="No revenue in this range" hint="Pick another date range or upload sales CSVs." />
      ) : (
        <div className="h-72 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={points} margin={{ top: 12, right: 8, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="revenueFill" x1="0" x2="0" y1="0" y2="1">
                  <stop offset="0%" stopColor={COLOR.revenue} stopOpacity={0.35} />
                  <stop offset="90%" stopColor={COLOR.revenue} stopOpacity={0} />
                </linearGradient>
                <linearGradient id="weekendFill" x1="0" x2="0" y1="0" y2="1">
                  <stop offset="0%" stopColor={COLOR.weekend} stopOpacity={0.12} />
                  <stop offset="100%" stopColor={COLOR.weekend} stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid vertical={false} stroke={COLOR.grid} strokeDasharray="2 4" strokeOpacity={0.6} />
              {weekends(points).map(([from, to]) => (
                <ReferenceArea key={from} x1={from} x2={to} fill="url(#weekendFill)" fillOpacity={1} strokeOpacity={0} />
              ))}
              <XAxis
                dataKey="day"
                tickFormatter={formatDayShort}
                tick={{ fill: COLOR.tick, fontSize: 11, fontFamily: "JetBrains Mono" }}
                axisLine={false}
                tickLine={false}
                minTickGap={24}
              />
              <YAxis
                tickFormatter={(rupees: number) => formatCompactINR(Math.round(rupees * 100))}
                tick={{ fill: COLOR.grid, fontSize: 11, fontFamily: "JetBrains Mono" }}
                axisLine={false}
                tickLine={false}
                width={64}
              />
              <Tooltip content={<TrendTooltip />} cursor={{ stroke: COLOR.revenue, strokeDasharray: "2 2" }} />
              <Area
                type="linear"
                dataKey="revenue"
                stroke={COLOR.revenue}
                strokeWidth={2.5}
                fill="url(#revenueFill)"
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="average"
                stroke={COLOR.average}
                strokeOpacity={0.8}
                strokeWidth={2}
                strokeDasharray="5 5"
                dot={false}
                isAnimationActive={false}
              />
              {peak && (
                <ReferenceDot x={peak.day} y={peak.revenue} r={5} fill={COLOR.revenue} stroke={COLOR.dotStroke} strokeWidth={2} />
              )}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
    </section>
  );
}
