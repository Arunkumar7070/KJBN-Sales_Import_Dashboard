import type { DashboardResponse } from "../../api/types";

export const RETURN_RATE_THRESHOLD_PERCENT = 5;

export interface DashboardTotals {
  netRevenuePaise: number;
  /** Sales only (payment split excludes returns). */
  salesRevenuePaise: number;
  /** Value refunded through returns: sales revenue minus net revenue. */
  refundedPaise: number;
  unitsSold: number;
  unitsReturned: number;
  returnRatePercent: number;
  activeStores: number;
  days: number;
}

const sum = (values: number[]) => values.reduce((total, value) => total + value, 0);

export function dashboardTotals(data: DashboardResponse): DashboardTotals {
  const netRevenuePaise = sum(data.store_revenue.map((store) => store.revenue.paise));
  const salesRevenuePaise = sum(data.payment_split.map((mode) => mode.revenue.paise));
  const unitsSold = sum(data.store_return_rates.map((store) => store.units_sold));
  const unitsReturned = sum(data.store_return_rates.map((store) => store.units_returned));
  return {
    netRevenuePaise,
    salesRevenuePaise,
    refundedPaise: salesRevenuePaise - netRevenuePaise,
    unitsSold,
    unitsReturned,
    returnRatePercent: unitsSold > 0 ? (unitsReturned / unitsSold) * 100 : 0,
    activeStores: data.store_return_rates.filter((store) => store.units_sold > 0).length,
    days: data.daily_revenue.length,
  };
}

export interface StoreHealth {
  code: string;
  name: string;
  revenuePaise: number;
  unitsSold: number;
  unitsReturned: number;
  /** Undefined when the store sold nothing in the range. */
  returnRatePercent: number | undefined;
}

/** Revenue and return rate per store, highest revenue first (ties in natural code order: ST2 before ST10). */
export function storeHealth(data: DashboardResponse): StoreHealth[] {
  const returnsByStore = new Map(data.store_return_rates.map((row) => [row.store_code, row]));
  const ordered = [...data.store_revenue].sort(
    (a, b) =>
      b.revenue.paise - a.revenue.paise || a.store_code.localeCompare(b.store_code, undefined, { numeric: true }),
  );
  return ordered.map((store) => {
    const returns = returnsByStore.get(store.store_code);
    const unitsSold = returns?.units_sold ?? 0;
    return {
      code: store.store_code,
      name: store.store_name,
      revenuePaise: store.revenue.paise,
      unitsSold,
      unitsReturned: returns?.units_returned ?? 0,
      returnRatePercent: returns && unitsSold > 0 ? returns.return_rate_percent : undefined,
    };
  });
}

export function isAboveReturnThreshold(store: StoreHealth): boolean {
  return store.returnRatePercent !== undefined && store.returnRatePercent >= RETURN_RATE_THRESHOLD_PERCENT;
}

/** Percent change from previous to current; undefined when there's no base. */
export function percentChange(current: number, previous: number): number | undefined {
  if (previous === 0) return undefined;
  return ((current - previous) / Math.abs(previous)) * 100;
}
