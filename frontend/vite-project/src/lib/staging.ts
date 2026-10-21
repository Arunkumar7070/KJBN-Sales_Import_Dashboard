// Client-side checks that mirror the backend's upload limits, so obvious
// problems are caught before uploading. The backend still validates everything.

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024; // MAX_UPLOAD_SIZE_BYTES
export const MAX_FILES_PER_UPLOAD = 10; // MAX_FILES_PER_UPLOAD

export interface StagedFile {
  id: string;
  file: File;
}

export interface StagingResult {
  staged: StagedFile[];
  skipped: { name: string; reason: string }[];
}

export function stageFiles(current: StagedFile[], incoming: File[], maxFiles: number): StagingResult {
  // A single-file dropzone replaces whatever was staged before.
  const staged = maxFiles === 1 ? [] : [...current];
  const skipped: StagingResult["skipped"] = [];
  for (const file of incoming) {
    if (!file.name.toLowerCase().endsWith(".csv")) {
      skipped.push({ name: file.name, reason: "not a .csv file" });
    } else if (file.size === 0) {
      skipped.push({ name: file.name, reason: "empty file" });
    } else if (file.size > MAX_UPLOAD_BYTES) {
      skipped.push({ name: file.name, reason: "larger than 10 MB" });
    } else if (staged.some((item) => item.file.name === file.name && item.file.size === file.size)) {
      skipped.push({ name: file.name, reason: "already staged" });
    } else if (staged.length >= maxFiles) {
      skipped.push({ name: file.name, reason: `limit of ${maxFiles} file${maxFiles === 1 ? "" : "s"}` });
    } else {
      staged.push({ id: crypto.randomUUID(), file });
    }
  }
  return { staged, skipped };
}

export function formatFileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
