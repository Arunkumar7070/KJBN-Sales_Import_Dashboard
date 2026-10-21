import { useState } from "react";
import { importStoreCatalog } from "../../api/client";
import type { StoreImportResult } from "../../api/types";
import { formatNumber } from "../../lib/format";
import { stageFiles, type StagedFile } from "../../lib/staging";
import Dropzone from "../upload/Dropzone";
import StagedFileList, { SkippedNotice, UploadProgress } from "../upload/StagedFileList";
import { ErrorPanel, Icon } from "../ui";

function MutationResult({ result }: { result: StoreImportResult }) {
  const chips: [string, number, string][] = [
    ["inserted", result.inserted, "bg-tertiary-container font-bold text-on-tertiary-container"],
    ["updated", result.updated, "bg-secondary-container font-bold text-on-secondary-fixed"],
    ["unchanged", result.unchanged, "bg-surface-container-highest text-on-surface-variant"],
    [
      "rejected",
      result.rejected,
      result.rejected > 0
        ? "bg-error-container font-bold text-on-error-container"
        : "bg-surface-container-highest text-on-surface-variant",
    ],
  ];
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-surface-container-high bg-surface-container-low p-3">
      <span className="text-[11px] font-semibold uppercase tracking-wider text-on-surface-variant">Mutation Result</span>
      <div className="grid grid-cols-2 gap-1 sm:grid-cols-4">
        {chips.map(([label, value, className]) => (
          <span key={label} className={`rounded px-1 py-1.5 text-center font-mono text-[11px] ${className}`}>
            {formatNumber(value)} {label}
          </span>
        ))}
      </div>
      {result.errors.length > 0 && (
        <div className="max-h-48 overflow-y-auto rounded bg-surface-container-lowest">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-surface-container text-[11px] font-semibold uppercase text-on-surface-variant">
              <tr>
                <th className="px-2 py-1.5">Row</th>
                <th className="px-2 py-1.5">Column</th>
                <th className="px-2 py-1.5">Message</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-surface-container-low">
              {result.errors.map((error) => (
                <tr key={error.row_number}>
                  <td className="px-2 py-1.5 font-mono text-on-surface">{error.row_number}</td>
                  <td className="px-2 py-1.5 font-mono text-on-surface-variant">{error.column_name}</td>
                  <td className="px-2 py-1.5 text-on-surface">{error.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** Upserts the store catalog (POST /api/v1/stores) from a code,name,city CSV. */
export default function StoreCatalogUpload() {
  const [staged, setStaged] = useState<StagedFile[]>([]);
  const [skipped, setSkipped] = useState<{ name: string; reason: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<Error>();
  const [result, setResult] = useState<StoreImportResult>();

  async function submit() {
    if (!staged[0]) return;
    setUploading(true);
    setProgress(0);
    setError(undefined);
    try {
      setResult(await importStoreCatalog(staged[0].file, setProgress));
      setStaged([]);
    } catch (caught) {
      setError(caught instanceof Error ? caught : new Error(String(caught)));
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="flex max-w-2xl flex-col gap-4 rounded-xl bg-surface-container-lowest p-6 shadow-sm">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="font-headline text-lg font-semibold tracking-[-0.01em] text-on-surface">Store Catalog Upload</h2>
          <span className="rounded bg-surface-container-low px-1 py-0.5 font-mono text-[11px] text-secondary">
            POST /api/v1/stores
          </span>
        </div>
        <span className="text-xs text-on-surface-variant">
          Master schema: <code className="font-mono font-medium text-primary">code,name,city</code> (any order). New codes
          are inserted, existing codes get their name and city updated, and stores missing from the file are left unchanged.
        </span>
      </div>

      <Dropzone
        compact
        icon="upload_file"
        multiple={false}
        disabled={uploading}
        onFiles={(files) => {
          const next = stageFiles(staged, files, 1);
          setStaged(next.staged);
          setSkipped(next.skipped);
          setResult(undefined);
        }}
        title="Select Stores CSV"
        hint="Single master catalog file, UTF-8 .csv up to 10 MB"
      />
      <SkippedNotice skipped={skipped} />
      {staged.length > 0 && (
        <StagedFileList files={staged} disabled={uploading} onRemove={() => setStaged([])} onClear={() => setStaged([])} />
      )}

      {uploading && <UploadProgress percent={progress} label="Uploading to /api/v1/stores…" />}
      {error && <ErrorPanel error={error} />}

      <button
        type="button"
        onClick={submit}
        disabled={staged.length === 0 || uploading}
        className="flex w-full items-center justify-center gap-1 rounded-lg bg-primary px-3 py-2 text-xs font-semibold tracking-[0.02em] text-on-primary shadow-sm transition-colors hover:bg-primary-container disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-primary"
      >
        <Icon name={uploading ? "progress_activity" : "sync"} className={`text-[16px] ${uploading ? "animate-spin" : ""}`} />
        {uploading ? "Syncing…" : "Upload & Sync Stores Catalog"}
      </button>

      {result && <MutationResult result={result} />}
    </div>
  );
}
