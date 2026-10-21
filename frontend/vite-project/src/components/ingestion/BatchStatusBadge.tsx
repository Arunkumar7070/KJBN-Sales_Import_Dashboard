import type { Batch, BatchStatus } from "../../api/types";

interface StatusStyle {
  label: string;
  badge: string;
  dot: string;
}

const STATUS_STYLE: Record<BatchStatus, StatusStyle> = {
  COMPLETED: { label: "Completed", badge: "bg-tertiary-container text-on-tertiary-container", dot: "bg-tertiary-fixed" },
  PROCESSING: {
    label: "Processing",
    badge: "bg-primary-container text-on-primary-container",
    dot: "bg-primary-fixed animate-pulse",
  },
  QUEUED: { label: "Queued", badge: "bg-secondary-container text-on-secondary-fixed", dot: "bg-secondary animate-pulse" },
  FAILED: { label: "Failed", badge: "bg-error-container text-on-error-container", dot: "bg-error" },
  REJECTED_HEADER: { label: "Header Rejected", badge: "bg-error-container text-on-error-container", dot: "bg-error" },
};

const COMPLETED_WITH_ERRORS: StatusStyle = {
  label: "Completed with Errors",
  badge: "bg-amber-100 text-amber-900",
  dot: "bg-amber-600",
};

export function isInFlight(batch: Batch): boolean {
  return batch.status === "QUEUED" || batch.status === "PROCESSING";
}

export default function BatchStatusBadge({ batch }: { batch: Batch }) {
  const style = batch.status === "COMPLETED" && batch.rows_rejected > 0 ? COMPLETED_WITH_ERRORS : STATUS_STYLE[batch.status];
  return (
    <span
      title={batch.failure_reason ?? undefined}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-semibold tracking-[0.03em] ${style.badge}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
      {style.label}
    </span>
  );
}
