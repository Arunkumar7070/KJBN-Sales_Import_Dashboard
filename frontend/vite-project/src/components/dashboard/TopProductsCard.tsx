import type { DashboardResponse } from "../../api/types";
import { formatINR, formatNumber } from "../../lib/format";
import { EmptyState } from "../ui";

export default function TopProductsCard({
  products,
  netUnits,
}: {
  products: DashboardResponse["top_products"];
  netUnits: number;
}) {
  const topUnits = products.reduce((sum, product) => sum + product.quantity_sold, 0);
  const share = netUnits > 0 ? (topUnits / netUnits) * 100 : 0;
  return (
    <section className="flex flex-col overflow-hidden rounded-xl bg-surface-container-lowest shadow-sm">
      <div className="flex flex-col justify-between gap-1 p-4 sm:flex-row sm:items-center">
        <h2 className="font-headline text-xl font-semibold tracking-[-0.015em] text-on-surface">Top 10 Products by Net Quantity</h2>
        <p className="text-xs text-on-surface-variant">Ranked by net units sold after customer returns</p>
      </div>
      {products.length === 0 ? (
        <EmptyState icon="inventory_2" title="No sales in this date range" />
      ) : (
        <div className="w-full overflow-x-auto">
          <table className="w-full border-collapse text-left">
            <thead>
              <tr className="h-9 bg-surface-container-low text-[11px] font-semibold uppercase tracking-wider text-on-surface-variant">
                <th className="w-16 px-3 text-center">Rank</th>
                <th className="w-36 px-3">SKU</th>
                <th className="px-3">Product Name</th>
                <th className="w-36 px-3 text-right">Net Units Sold</th>
                <th className="w-48 px-3 text-right">Net Revenue (₹)</th>
              </tr>
            </thead>
            <tbody className="text-sm text-on-surface">
              {products.map((product, index) => (
                <tr
                  key={product.sku}
                  className={`h-11 transition-colors hover:bg-surface-container-low/60 ${index % 2 ? "bg-surface-container-low/20" : ""}`}
                >
                  <td className={`px-3 text-center text-xs font-bold ${product.rank <= 3 ? "text-primary" : "text-on-surface-variant"}`}>
                    {product.rank}
                  </td>
                  <td className="whitespace-nowrap px-3 font-mono text-xs font-medium text-primary">{product.sku}</td>
                  <td className="max-w-[320px] truncate px-3 font-semibold" title={product.product_name}>
                    {product.product_name}
                  </td>
                  <td className="px-3 text-right font-mono text-xs font-bold">{formatNumber(product.quantity_sold)}</td>
                  <td className={`px-3 text-right font-mono text-xs font-bold ${product.revenue.paise < 0 ? "text-error" : ""}`}>
                    {formatINR(product.revenue.paise, 0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {products.length > 0 && (
        <div className="flex flex-col items-center justify-between gap-2 bg-surface-container-low px-4 py-2 sm:flex-row">
          <span className="font-mono text-xs text-on-surface-variant">
            Top {products.length} products: {formatNumber(topUnits)} net units
          </span>
          <span className="text-[11px] font-bold tracking-[0.03em] text-primary">{share.toFixed(1)}% of all net units</span>
        </div>
      )}
    </section>
  );
}
