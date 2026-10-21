import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { uploadSalesFiles } from "../api/client";
import type { UploadFileStatus, UploadResult } from "../api/types";
import BatchHistory from "../components/ingestion/BatchHistory";
import StoreCatalogUpload from "../components/stores/StoreCatalogUpload";
import Dropzone from "../components/upload/Dropzone";
import StagedFileList, { SkippedNotice, UploadProgress } from "../components/upload/StagedFileList";
import { ErrorPanel, Icon } from "../components/ui";
import { shortId } from "../lib/format";
import { MAX_FILES_PER_UPLOAD, stageFiles, type StagedFile } from "../lib/staging";

const REQUIRED_COLUMNS = [
  "transaction_id",
  "store_code",
  "sold_at",
  "sku",
  "product_name",
  "quantity",
  "unit_price",
  "discount",
  "payment_mode",
  "original_transaction_id",
];

type Tab = "sales" | "stores";

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: "sales", label: "Sales Transactions Upload", icon: "receipt_long" },
  { id: "stores", label: "Store Catalog Upload", icon: "storefront" },
];

const RESULT_STYLE: Record<UploadFileStatus, { label: string; className: string; icon: string }> = {
  QUEUED: { label: "Queued", className: "bg-primary-fixed text-on-primary-fixed", icon: "schedule_send" },
  "ALREADY UPLOADED": {
    label: "Already uploaded",
    className: "bg-secondary-container text-on-secondary-fixed",
    icon: "content_copy",
  },
  INVALID: { label: "Invalid", className: "bg-error-container text-on-error-container", icon: "block" },
};

function UploadResults({ result }: { result: UploadResult }) {
  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs font-semibold tracking-[0.02em] text-on-surface">
        Upload Results <span className="font-normal text-on-surface-variant">· progress appears in the ledger below</span>
      </span>
      <ul className="flex flex-col gap-1">
        {result.files.map((file, index) => {
          const style = RESULT_STYLE[file.status];
          return (
            <li key={`${file.filename}-${index}`} className="flex items-start justify-between gap-3 rounded-lg bg-surface-container-low p-2">
              <div className="flex min-w-0 items-start gap-3">
                <Icon name={style.icon} className="mt-0.5 shrink-0 text-[18px] text-on-surface-variant" />
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs font-medium text-on-surface">{file.filename}</p>
                  <p className="break-words text-xs text-on-surface-variant">{file.message}</p>
                </div>
              </div>
              <span className={`shrink-0 whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-semibold ${style.className}`}>
                {style.label}
                {file.batch_id && <span className="font-mono"> #{shortId(file.batch_id)}</span>}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function SalesUpload({ onUploaded }: { onUploaded: () => void }) {
  const [staged, setStaged] = useState<StagedFile[]>([]);
  const [skipped, setSkipped] = useState<{ name: string; reason: string }[]>([]);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [uploadError, setUploadError] = useState<Error>();
  const [result, setResult] = useState<UploadResult>();

  function addFiles(files: File[]) {
    const next = stageFiles(staged, files, MAX_FILES_PER_UPLOAD);
    setStaged(next.staged);
    setSkipped(next.skipped);
  }

  async function upload() {
    setUploading(true);
    setProgress(0);
    setUploadError(undefined);
    try {
      setResult(await uploadSalesFiles(staged.map((item) => item.file), setProgress));
      setStaged([]);
      setSkipped([]);
      onUploaded();
    } catch (caught) {
      // 429 / 503 / network: keep the staged files so the user can retry.
      setUploadError(caught instanceof Error ? caught : new Error(String(caught)));
    } finally {
      setUploading(false);
    }
  }

  return (
    <div className="flex flex-col gap-4 rounded-xl bg-surface-container-lowest p-6 shadow-sm">
      <div className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="font-headline text-lg font-semibold tracking-[-0.01em] text-on-surface">Upload Sales Batches</h2>
          <span className="text-xs text-on-surface-variant">UTF-8 encoded .csv files up to 10 MB</span>
        </div>
        <p className="text-xs text-on-surface-variant">
          Required headers (any order):{" "}
          <span className="font-mono text-on-surface">{REQUIRED_COLUMNS.join(", ")}</span>. <code className="font-mono">sold_at</code>{" "}
          is IST; amounts are in rupees. Re-uploads of the same file are detected and never imported twice.
        </p>
      </div>
      <Dropzone
        multiple
        disabled={uploading}
        onFiles={addFiles}
        title="Drag & drop sales CSV files here, or browse"
        hint={`Supports single or multi-file batch uploads (up to ${MAX_FILES_PER_UPLOAD} files)`}
      />
      <SkippedNotice skipped={skipped} />
      <StagedFileList
        files={staged}
        disabled={uploading}
        onRemove={(id) => setStaged(staged.filter((item) => item.id !== id))}
        onClear={() => setStaged([])}
      />
      {uploading && <UploadProgress percent={progress} label="Uploading to /api/v1/imports…" />}
      {uploadError && <ErrorPanel error={uploadError} />}
      {result && !uploading && <UploadResults result={result} />}
      <div className="flex justify-end">
        <button
          type="button"
          onClick={upload}
          disabled={staged.length === 0 || uploading}
          className="flex items-center gap-1 rounded-lg bg-primary px-4 py-2 text-xs font-semibold tracking-[0.02em] text-on-primary shadow-sm transition-colors hover:bg-primary-container disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-primary"
        >
          <Icon name={uploading ? "progress_activity" : "upload"} className={`text-[16px] ${uploading ? "animate-spin" : ""}`} />
          {uploading ? "Uploading…" : "Upload Files"}
        </button>
      </div>
    </div>
  );
}

export default function IngestionPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tab: Tab = searchParams.get("tab") === "stores" ? "stores" : "sales";
  const [refreshKey, setRefreshKey] = useState(0);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1">
        <h1 className="font-headline text-[1.75rem] font-bold leading-9 tracking-[-0.02em] text-on-surface">Data Ingestion</h1>
        <p className="max-w-2xl text-sm text-on-surface-variant">
          Upload sales transaction batches and master store catalog CSV files for automated schema validation and
          ingestion.
        </p>
      </div>

      <div role="tablist" aria-label="Upload type" className="flex items-center gap-3 border-b border-surface-container-high">
        {TABS.map((item) => {
          const active = item.id === tab;
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              id={`tab-${item.id}`}
              aria-selected={active}
              aria-controls={`panel-${item.id}`}
              onClick={() => setSearchParams(item.id === "sales" ? {} : { tab: item.id }, { replace: true })}
              className={`-mb-px flex items-center gap-1 border-b-2 pb-2 text-sm font-semibold tracking-[0.01em] transition-colors ${
                active ? "border-primary text-primary" : "border-transparent text-on-surface-variant hover:text-on-surface"
              }`}
            >
              <Icon name={item.icon} className="text-[16px]" />
              {item.label}
            </button>
          );
        })}
      </div>

      {/* Both panels stay mounted so staged files survive switching tabs. */}
      <div role="tabpanel" id="panel-sales" aria-labelledby="tab-sales" hidden={tab !== "sales"} className="flex flex-col gap-6">
        <SalesUpload onUploaded={() => setRefreshKey((key) => key + 1)} />
        <BatchHistory refreshKey={refreshKey} />
      </div>
      <div role="tabpanel" id="panel-stores" aria-labelledby="tab-stores" hidden={tab !== "stores"}>
        <StoreCatalogUpload />
      </div>
    </div>
  );
}
