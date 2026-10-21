import type { ReactNode } from "react";
import { ApiError } from "../api/client";

export function Icon({ name, className = "" }: { name: string; className?: string }) {
  return (
    <span aria-hidden="true" className={`material-symbols-outlined ${className}`}>
      {name}
    </span>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-xl bg-surface-container-lowest shadow-sm ${className}`}>{children}</section>;
}

export function Spinner({ label }: { label: string }) {
  return (
    <div role="status" className="flex items-center justify-center gap-2 py-10 text-on-surface-variant">
      <Icon name="progress_activity" className="animate-spin text-[20px] text-primary" />
      <span className="text-sm">{label}</span>
    </div>
  );
}

export function EmptyState({
  icon = "inbox",
  title,
  hint,
  action,
}: {
  icon?: string;
  title: string;
  hint?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-4 py-10 text-center">
      <div className="flex h-11 w-11 items-center justify-center rounded-full bg-surface-container text-on-surface-variant">
        <Icon name={icon} className="text-[22px]" />
      </div>
      <p className="font-medium text-on-surface">{title}</p>
      {hint && <p className="max-w-md text-xs text-on-surface-variant">{hint}</p>}
      {action}
    </div>
  );
}

/** A clear error message, the request ID to quote, and an optional retry. */
export function ErrorPanel({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  const requestId = error instanceof ApiError ? error.requestId : null;
  return (
    <div
      role="alert"
      className="flex flex-col gap-3 rounded-lg bg-error-container/20 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
    >
      <div className="flex items-start gap-2">
        <Icon name="error" className="mt-0.5 text-[18px] text-error" />
        <div>
          <p className="text-sm text-on-surface">{error.message}</p>
          {requestId && (
            <p className="mt-0.5 font-mono text-[11px] text-on-surface-variant">Request ID: {requestId}</p>
          )}
        </div>
      </div>
      {onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="flex shrink-0 items-center gap-1 self-start rounded bg-surface-container-high px-3 py-1.5 text-xs text-on-surface transition-colors hover:bg-surface-variant sm:self-auto"
        >
          <Icon name="refresh" className="text-[16px]" />
          Retry
        </button>
      )}
    </div>
  );
}

export const primaryButton =
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded-xl bg-primary px-4 py-2 text-xs font-semibold tracking-[0.02em] text-on-primary shadow-sm transition-colors hover:bg-primary-container disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-primary";