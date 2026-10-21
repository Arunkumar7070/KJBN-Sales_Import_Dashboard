// Response shapes of the FastAPI backend (backend/app/schemas.py).

/** Money is always exact integer paise; `rupees` is the same value as a string. */
export interface Money {
  paise: number;
  rupees: string;
}

export type PaymentMode = "CASH" | "UPI" | "CARD";

export type BatchStatus =
  | "QUEUED"
  | "PROCESSING"
  | "COMPLETED"
  | "REJECTED_HEADER"
  | "FAILED";

export interface Store {
  code: string;
  name: string;
  city: string;
}

export interface DashboardResponse {
  daily_revenue: { day: string; revenue: Money }[];
  store_revenue: { store_code: string; store_name: string; revenue: Money }[];
  top_products: {
    rank: number;
    sku: string;
    product_name: string;
    quantity_sold: number;
    revenue: Money;
  }[];
  store_return_rates: {
    store_code: string;
    units_sold: number;
    units_returned: number;
    return_rate_percent: number;
  }[];
  payment_split: {
    payment_mode: PaymentMode;
    transaction_count: number;
    revenue: Money;
    share_percent: number;
  }[];
}

export interface Batch {
  id: string;
  filename: string;
  status: BatchStatus;
  uploaded_at: string;
  started_at: string | null;
  completed_at: string | null;
  rows_total: number;
  rows_accepted: number;
  rows_rejected: number;
  attempt_count: number;
  failure_reason: string | null;
  /** Path relative to the API base URL, or null when no rows were rejected. */
  error_report_url: string | null;
}

export interface BatchList {
  items: Batch[];
  total: number;
  limit: number;
  offset: number;
}

export interface Transaction {
  transaction_id: string;
  store_code: string;
  sold_at: string;
  sku: string;
  product_name: string;
  quantity: number;
  unit_price: Money;
  discount: Money;
  line_total: Money;
  payment_mode: PaymentMode;
  is_return: boolean;
  original_transaction_id: string | null;
  batch_id: string;
  created_at: string;
}

export interface TransactionList {
  items: Transaction[];
  total: number;
  page: number;
  limit: number;
  pages: number;
}

export type UploadFileStatus = "QUEUED" | "ALREADY UPLOADED" | "INVALID";

export interface UploadResult {
  files: {
    filename: string;
    status: UploadFileStatus;
    batch_id: string | null;
    message: string;
  }[];
  queued_batch_ids: string[];
}

export interface StoreImportResult {
  rows_total: number;
  inserted: number;
  updated: number;
  unchanged: number;
  rejected: number;
  errors: { row_number: number; column_name: string; message: string }[];
}

export interface ApiErrorBody {
  error: {
    status: number;
    code: string;
    message: string;
    request_id: string | null;
    details?: { field: string; message: string; type: string }[];
  };
}
