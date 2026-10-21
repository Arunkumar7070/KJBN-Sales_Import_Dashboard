import { formatFileSize, type StagedFile } from "../../lib/staging";
import { Icon } from "../ui";

export function SkippedNotice({ skipped }: { skipped: { name: string; reason: string }[] }) {
  if (skipped.length === 0) return null;
  return (
    <div role="status" className="rounded-lg bg-amber-100 px-3 py-2 text-xs text-amber-900">
      Skipped {skipped.length} file{skipped.length === 1 ? "" : "s"}:{" "}
      {skipped.map((item) => `${item.name} (${item.reason})`).join(", ")}
    </div>
  );
}

export default function StagedFileList({
  files,
  disabled,
  onRemove,
  onClear,
}: {
  files: StagedFile[];
  disabled: boolean;
  onRemove: (id: string) => void;
  onClear: () => void;
}) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold tracking-[0.02em] text-on-surface">Staged Files ({files.length})</span>
        {files.length > 0 && (
          <button
            type="button"
            onClick={onClear}
            disabled={disabled}
            className="flex items-center gap-1 text-[11px] font-semibold tracking-[0.03em] text-error hover:underline disabled:opacity-40"
          >
            <Icon name="clear" className="text-[14px]" />
            Clear All
          </button>
        )}
      </div>
      {files.length === 0 ? (
        <p className="py-3 text-center text-xs text-on-surface-variant">No files staged yet. Drag .csv files above.</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {files.map(({ id, file }) => (
            <li key={id} className="flex items-center justify-between gap-3 rounded-lg bg-surface-container-low p-2">
              <div className="flex min-w-0 items-center gap-3">
                <Icon name="description" className="shrink-0 text-[20px] text-primary" />
                <div className="flex min-w-0 flex-col">
                  <span className="truncate font-mono text-xs font-medium text-on-surface">{file.name}</span>
                  <span className="text-xs text-on-surface-variant">{formatFileSize(file.size)}</span>
                </div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <span className="rounded bg-tertiary-container px-1 py-0.5 text-[11px] font-semibold tracking-[0.03em] text-on-tertiary-container">
                  Ready
                </span>
                <button
                  type="button"
                  onClick={() => onRemove(id)}
                  disabled={disabled}
                  aria-label={`Remove ${file.name}`}
                  className="flex p-1 text-on-surface-variant transition-colors hover:text-error disabled:opacity-40"
                >
                  <Icon name="close" className="text-[16px]" />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function UploadProgress({ percent, label }: { percent: number; label: string }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-lg bg-surface-container-low p-3">
      <div className="flex items-center justify-between text-xs">
        <span className="flex items-center gap-1.5 text-on-surface">
          <span className="h-2 w-2 animate-pulse rounded-full bg-primary-container" />
          {percent < 100 ? label : "Upload complete — waiting for the server…"}
        </span>
        <span className="font-mono font-semibold text-primary">{percent}%</span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-container-high">
        <div className="h-full bg-primary-container transition-all duration-300" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}
