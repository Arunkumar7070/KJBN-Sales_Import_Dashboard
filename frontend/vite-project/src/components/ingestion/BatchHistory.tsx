import { useEffect, useState } from "react";
import { apiUrl, listImports } from "../../api/client";
import type { Batch } from "../../api/types";
import { formatIstDateTime, formatNumber } from "../../lib/format";
import { useAsync } from "../../lib/useAsync";
import { usePolling } from "../../lib/usePolling";
import Pagination from "../Pagination";
import { EmptyState, ErrorPanel, Icon, Spinner } from "../ui";
import BatchStatusBadge, { isInFlight } from "./BatchStatusBadge";

const PAGE_SIZE = 10;

const HEADERS: [string, string][] = [
  ["Filename", ""],
  ["Uploaded At (IST)", ""],
  ["Total Rows", "text-right"],
  ["Accepted", "text-right"],
  ["Rejected", "text-right"],
  ["Status", ""],
  ["Error Report Action", "text-right"],
];

function BatchRow({ batch }: { batch: Batch }) {
  // Rows are only counted once the batch has been imported.
  const counted = batch.status === "COMPLETED";
  return (
    <tr className="transition-colors hover:bg-surface-container-low/60">
      <td className="px-3 py-2">
        <div className="flex flex-col">
          <span className="max-w-[360px] truncate font-mono font-medium text-on-surface" title={`${batch.filename} · ${batch.id}`}>
            {batch.filename}
          </span>
          {batch.failure_reason && (
            <span className="max-w-[360px] truncate text-[11px] text-error" title={batch.failure_reason}>
              {batch.failure_reason}
            </span>
          )}
        </div>
      </td>
      <td className="whitespace-nowrap px-3 py-2 text-on-surface-variant">{formatIstDateTime(batch.uploaded_at)}</td>
      <td className="px-3 py-2 text-right font-mono font-medium text-on-surface">
        {counted ? formatNumber(batch.rows_total) : "—"}
      </td>
      <td className="px-3 py-2 text-right font-mono font-semibold text-tertiary">
        {counted ? formatNumber(batch.rows_accepted) : "—"}
      </td>
      <td className="px-3 py-2 text-right font-mono text-on-surface-variant">
        {!counted ? (
          "—"
        ) : batch.rows_rejected > 0 ? (
          <span className="rounded bg-error-container px-1 py-0.5 text-[11px] font-semibold text-on-error-container">
            {formatNumber(batch.rows_rejected)}
          </span>
        ) : (
          "0"
        )}
      </td>
      <td className="px-3 py-2">
        <BatchStatusBadge batch={batch} />
      </td>
      <td className="px-3 py-2 text-right">
        {batch.error_report_url ? (
          <a
            href={apiUrl(batch.error_report_url)}
            download
            className="inline-flex items-center gap-1 whitespace-nowrap text-[11px] font-semibold tracking-[0.03em] text-primary transition-colors hover:text-primary-container"
          >
            <Icon name="download" className="text-[16px]" />
            Download errors.csv
          </a>
        ) : (
          <span className="font-mono text-on-surface-variant">—</span>
        )}
      </td>
    </tr>
  );
}

export default function BatchHistory({ refreshKey }: { refreshKey: number }) {
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const { data, error, loading, reload } = useAsync(
    (signal) => listImports(PAGE_SIZE, (page - 1) * PAGE_SIZE, signal),
    [page, refreshKey],
  );
  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  // A new upload appears on page 1, so jump there after each upload.
  useEffect(() => setPage(1), [refreshKey]);
  usePolling(reload, Boolean(data?.items.some(isInFlight)));

  const term = search.trim().toLowerCase();
  const items = (data?.items ?? []).filter(
    (batch) => !term || batch.filename.toLowerCase().includes(term) || batch.id.toLowerCase().includes(term),
  );
  const firstRow = data && data.total > 0 ? (page - 1) * PAGE_SIZE + 1 : 0;
  const lastRow = data ? Math.min(page * PAGE_SIZE, data.total) : 0;

  return (
    <section className="flex flex-col overflow-hidden rounded-xl bg-surface-container-lowest shadow-sm">
      <div className="flex flex-col justify-between gap-3 bg-surface-container-low p-3 sm:flex-row sm:items-center">
        <div className="flex items-center gap-2">
          <Icon name="history" className="text-[20px] text-primary" />
          <h3 className="font-headline text-lg font-semibold tracking-[-0.01em] text-on-surface">Batch History Ledger</h3>
        </div>
        <div className="flex items-center gap-2">
          <div className="relative w-full sm:w-64">
            <Icon name="search" className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[16px] text-on-surface-variant" />
            <input
              type="search"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Filter batch…"
              aria-label="Filter this page by filename or batch ID"
              className="w-full rounded-lg bg-surface-container-lowest py-1.5 pl-8 pr-3 text-xs text-on-surface shadow-sm placeholder:text-on-surface-variant/70 focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          <button
            type="button"
            onClick={reload}
            disabled={loading}
            title="Refresh"
            aria-label="Refresh batch history"
            className="flex rounded-lg bg-surface-container-lowest p-1.5 text-on-surface-variant shadow-sm transition-colors hover:text-primary disabled:opacity-50"
          >
            <Icon name="refresh" className={`text-[18px] ${loading ? "animate-spin" : ""}`} />
          </button>
        </div>
      </div>

      {error && (
        <div className="p-3">
          <ErrorPanel error={error} onRetry={reload} />
        </div>
      )}

      {!data && loading ? (
        <Spinner label="Loading batch history…" />
      ) : data && data.total === 0 ? (
        <EmptyState icon="history" title="No batches yet" hint="Uploaded sales files will appear here with their import results." />
      ) : (
        data && (
          <div className="w-full overflow-x-auto">
            <table className="w-full min-w-[900px] border-collapse text-left text-xs">
              <thead>
                <tr className="bg-surface-container-low text-[11px] font-semibold uppercase tracking-wider text-on-surface-variant">
                  {HEADERS.map(([label, className]) => (
                    <th key={label} className={`px-3 py-2 font-semibold ${className}`}>
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-surface-container-low">
                {items.length === 0 && (
                  <tr>
                    <td colSpan={HEADERS.length} className="px-3 py-8 text-center text-on-surface-variant">
                      No batches on this page match “{search}”.
                    </td>
                  </tr>
                )}
                {items.map((batch) => (
                  <BatchRow key={batch.id} batch={batch} />
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {data && data.total > 0 && (
        <div className="flex flex-col items-center justify-between gap-3 bg-surface-container-low p-2 px-3 sm:flex-row">
          <span className="text-[11px] font-semibold tracking-[0.03em] text-on-surface-variant">
            Showing {formatNumber(firstRow)}–{formatNumber(lastRow)} of {formatNumber(data.total)} import batches
          </span>
          <Pagination page={page} pages={pages} onPage={setPage} />
        </div>
      )}
    </section>
  );
}
