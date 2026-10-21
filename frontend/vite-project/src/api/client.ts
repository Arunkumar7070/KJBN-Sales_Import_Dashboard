import type {
  ApiErrorBody,
  BatchList,
  DashboardResponse,
  Store,
  StoreImportResult,
  TransactionList,
  UploadResult,
} from "./types";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

/** Backend origin, from VITE_API_BASE_URL (no trailing slash). */
export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE_URL
).replace(/\/+$/, "");

/** An absolute URL for a backend path such as Batch.error_report_url. */
export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

export class ApiError extends Error {
  readonly status: number;
  readonly requestId: string | null;

  constructor(status: number, message: string, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.requestId = requestId;
  }
}

const UNREACHABLE_MESSAGE = `Cannot reach the API at ${API_BASE_URL}. Is the backend running?`;

type QueryValue = string | number | boolean | null | undefined;

/** "?a=1&b=x" from the defined, non-empty values (or "" if there are none). */
export function queryString(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

async function errorFromResponse(response: Response): Promise<ApiError> {
  const headerId = response.headers.get("X-Request-ID");
  try {
    const body = (await response.json()) as Partial<ApiErrorBody>;
    if (body.error?.message) {
      return new ApiError(response.status, body.error.message, body.error.request_id ?? headerId);
    }
  } catch {
    // Not JSON (e.g. a proxy error page): fall through to a generic message.
  }
  return new ApiError(response.status, `Request failed (HTTP ${response.status}).`, headerId);
}

async function send(path: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(apiUrl(path), init);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, UNREACHABLE_MESSAGE);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await send(path, init);
  if (!response.ok) throw await errorFromResponse(response);
  return (await response.json()) as T;
}

// Read endpoints

export function getDashboard(
  dateFrom: string | undefined,
  dateTo: string | undefined,
  signal?: AbortSignal,
): Promise<DashboardResponse> {
  return request(
    `/api/v1/analytics/dashboard${queryString({ date_from: dateFrom, date_to: dateTo })}`,
    { signal },
  );
}

/** Stores in natural code order (ST2 before ST10), which the API's text sort doesn't give. */
export async function listStores(signal?: AbortSignal): Promise<Store[]> {
  const stores = await request<Store[]>("/api/v1/stores", { signal });
  return stores.sort((a, b) => a.code.localeCompare(b.code, undefined, { numeric: true }));
}

export function listImports(limit: number, offset: number, signal?: AbortSignal): Promise<BatchList> {
  return request(`/api/v1/imports${queryString({ limit, offset })}`, { signal });
}

export interface TransactionFilters {
  storeCode?: string;
  sku?: string;
  dateFrom?: string;
  dateTo?: string;
  returnsOnly?: boolean;
}

export function transactionsPath(filters: TransactionFilters, page: number, limit: number): string {
  return `/api/v1/transactions${queryString({
    store_code: filters.storeCode,
    sku: filters.sku?.trim(),
    date_from: filters.dateFrom,
    date_to: filters.dateTo,
    returns_only: filters.returnsOnly ? true : undefined,
    page,
    limit,
  })}`;
}

export function listTransactions(
  filters: TransactionFilters,
  page: number,
  limit: number,
  signal?: AbortSignal,
): Promise<TransactionList> {
  return request(transactionsPath(filters, page, limit), { signal });
}

// Uploads (XMLHttpRequest, so the UI can show real upload progress)

function uploadForm<T>(
  path: string,
  form: FormData,
  onProgress: ((percent: number) => void) | undefined,
  acceptStatus: (status: number) => boolean,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", apiUrl(path));
    xhr.responseType = "json";
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress?.(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onerror = () => reject(new ApiError(0, UNREACHABLE_MESSAGE));
    xhr.onload = () => {
      const body = xhr.response as (T & Partial<ApiErrorBody>) | null;
      if (acceptStatus(xhr.status) && body) {
        resolve(body);
        return;
      }
      const retryAfter = xhr.getResponseHeader("Retry-After");
      const message =
        body?.error?.message ??
        (xhr.status === 429 && retryAfter
          ? `Too many uploads. Try again in ${retryAfter} seconds.`
          : `Upload failed (HTTP ${xhr.status}).`);
      reject(
        new ApiError(xhr.status, message, body?.error?.request_id ?? xhr.getResponseHeader("X-Request-ID")),
      );
    };
    xhr.send(form);
  });
}

/**
 * POST /api/v1/imports. The backend answers 202 (something queued), 200 (all
 * already uploaded) or 400 (nothing usable) with per-file results in all three
 * cases, so those resolve; 429/503 and others reject with an ApiError.
 */
export function uploadSalesFiles(files: File[], onProgress?: (percent: number) => void): Promise<UploadResult> {
  const form = new FormData();
  for (const file of files) form.append("files", file);
  return uploadForm<UploadResult>("/api/v1/imports", form, onProgress, (status) =>
    [200, 202, 400].includes(status),
  );
}

/** POST /api/v1/stores: upsert the store catalog (columns code,name,city). */
export function importStoreCatalog(file: File, onProgress?: (percent: number) => void): Promise<StoreImportResult> {
  const form = new FormData();
  form.append("file", file);
  return uploadForm<StoreImportResult>("/api/v1/stores", form, onProgress, (status) => status === 200);
}
