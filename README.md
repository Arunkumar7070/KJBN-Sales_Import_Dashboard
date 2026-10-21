# KJBN Sales Import & Dashboard

**Problem 5: Sales Import & Dashboard** · KJBN Labs Software Engineering Internship take-home

A web app that ingests the daily sales CSVs of a six-store retail chain **row by row**, then shows revenue, returns and payment trends on a dashboard. It rejects bad rows with a row, column and reason, never imports the same file or transaction twice, and survives two concurrent uploads of the same file.

**Contents:** [Live links](#2-live-links) · [Tech stack](#3-tech-stack) · [Architecture](#4-architecture) · [Running locally](#5-running-locally) · [Deployment](#5a-deployment) · [Tests & coverage](#6-running-tests-and-coverage) · [API](#7-api-overview) · [Rules & assumptions](#8-business-rules-and-assumptions) · [Errors & logging](#9-error-handling-and-logging) · [Reviewer aids](#10-reviewer-aids) · [Limitations](#11-known-limitations) · [AI usage](#12-ai-and-third-party-usage)

## 1. Title and one-liner

**KJBN Sales Import & Dashboard**: Problem 5 (Sales Import & Dashboard). Idempotent, row-level-validated CSV ingestion for a retail chain, with a reporting dashboard over the imported transactions.

## 2. Live links

| What | URL |
| --- | --- |
| UI | https://vite-project-lovat-mu.vercel.app/ingestion |
| API base URL | https://kjbn-sales-backend.onrender.com |
| API docs (Swagger) | https://kjbn-sales-backend.onrender.com/docs#/ |
| Health | https://kjbn-sales-backend.onrender.com/health · https://kjbn-sales-backend.onrender.com/health/db |

- **No authentication**, so there are no test credentials. Anyone with the URL can upload (see §11).
- **Seed data is loaded:** 5,003 sales across the 6 stores, from 1 Sep to 20 Oct 2026. The hosted catalog also holds the 100-store sample catalog (`Sample_Data/stores_100.csv`) and one upload of `sample_sales.csv`.
- **Cold start:** the backend runs on Render's free tier and sleeps after inactivity, so the first request after a pause takes **30–60 s**. The Neon database also suspends when idle and wakes on the first query. Both recover on their own; just retry.

## 3. Tech stack

| Layer | Choice | Hosting |
| --- | --- | --- |
| Backend | Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2 (psycopg 3) · Alembic | Render free web service (Singapore) |
| Database | PostgreSQL (18 on Neon; 16 in Docker Compose) | Neon free tier |
| Frontend | TypeScript · React 19 · React Router 7 · Recharts 3 · Tailwind CSS 4 · Vite 8 | Vercel |
| Tests / lint | pytest · pytest-cov · ruff | GitHub Actions |
| Local stack | Docker Compose: Postgres 16 + backend + nginx-served UI | — |

- **Stack choice:** every choice is from the recommended stacks.
- **Money** is stored as **integer paise** (BIGINT).
- **Timestamps** are stored in **UTC** and displayed in **IST**.

## 4. Architecture

### 4.1 Components and request flow

```mermaid
flowchart LR
    subgraph Browser["Browser · Vercel"]
        UI["React SPA<br/>src/api/client.ts is the only API caller"]
    end

    subgraph API["FastAPI · Render"]
        MW["Middleware<br/>X-Request-ID · JSON request log<br/>X-Test-Clock · CORS"]
        R["Routes · backend/api/routes.py<br/>parse → call service → shape response"]
        subgraph SVC["Services · backend/app/services/ (all business rules)"]
            W["import_worker.py<br/>hash, stage, claim, retry, CLI"]
            P["csv_parser.py<br/>file, encoding, header and per-row field checks<br/><i>pure, no DB</i>"]
            I["sales_importer.py<br/>lookups, two-pass plan, chunked inserts<br/><i>one transaction</i>"]
            S["sales_rules.py<br/>sale and return rules for one row<br/><i>pure, no DB</i>"]
            ST["store_import.py<br/>store catalog upsert"]
            A["analytics.py<br/>dashboard SQL"]
        end
        M["SQLAlchemy models · backend/app/models.py"]
    end

    subgraph DB["PostgreSQL · Neon"]
        T[("stores · products · product_aliases<br/>import_batches · import_payloads<br/>import_row_errors · transactions")]
    end

    UI -- "JSON over HTTPS<br/>VITE_API_BASE_URL" --> MW --> R
    R --> W & ST & A
    W --> P & I
    I --> S
    W & I & ST & A --> M --> T
```

ASCII equivalent, for terminals:

```
Browser (React SPA) ──JSON/HTTPS──▶ FastAPI
                                      ├─ middleware: request id, JSON log, test clock, CORS
                                      ├─ api/routes.py ............ HTTP only
                                      └─ app/services/ ............ every business rule
                                           ├─ import_worker.py         hash, stage, claim, retry, CLI
                                           ├─ csv_parser.py   (pure)   file/encoding/header/field checks
                                           ├─ sales_importer.py        lookups, plan, chunked inserts
                                           ├─ sales_rules.py  (pure)   sale and return rules
                                           ├─ store_import.py          store catalog upsert
                                           └─ analytics.py             dashboard SQL
                                                 │
                                           app/models.py ──▶ PostgreSQL (Alembic migrations)
```

### 4.2 Upload lifecycle (rules 1 and 6)

```mermaid
sequenceDiagram
    participant B as Browser
    participant API as POST /api/v1/imports
    participant DB as PostgreSQL
    participant BG as Background task (same process)

    B->>API: multipart files[]
    API->>API: per file: extension and size → SHA-256 of the bytes
    API->>DB: INSERT import_batches (QUEUED)<br/>UNIQUE(file_sha256)
    alt hash already known
        DB-->>API: conflict → existing batch
        API-->>B: file reported ALREADY UPLOADED (nothing changes)
    else new file
        API->>DB: INSERT import_payloads (gzipped bytes)
        API-->>B: 202 Accepted, per-file results
        API-)BG: schedule import
        BG->>DB: UPDATE … SET status=PROCESSING WHERE status is claimable
        Note over BG,DB: atomic claim, so a second worker gets 0 rows and stops
        BG->>BG: decode UTF-8, parse CSV, check header (bad header → REJECTED_HEADER)
        BG->>DB: load stores, known transaction_ids, originals (FOR UPDATE)
        BG->>BG: pass 1: validate sales · pass 2: validate returns (in memory)
        BG->>DB: 1,000-row multi-row INSERTs: products, aliases, transactions, row errors
        BG->>DB: batch → COMPLETED, payload deleted, same transaction, COMMIT
    end
```

**Why this shape:**
- **No timeout (rule 6):** the HTTP request only checks, hashes and stages each file, so even a 20,000-row upload returns at once with 202.
- **Idempotency (rule 1):** the UNIQUE hash catches a repeated file without a lookup race.
- **Concurrency:** the atomic claim means two concurrent uploads of the same file can only ever run one import.
- **No partial imports:** everything the worker writes is in one transaction, so a crash mid-batch leaves no partial rows.

### 4.3 Batch states

| Status | Meaning | Set by |
| --- | --- | --- |
| `QUEUED` | Staged; payload stored; not yet claimed | `import_worker.register_import_batch` (via `enqueue_upload`) |
| `PROCESSING` | Claimed by exactly one worker | `import_worker.claim_batch` (atomic UPDATE) |
| `COMPLETED` | Rows written; counts and error report final; payload deleted | `sales_importer.process_batch` |
| `REJECTED_HEADER` | Header has a missing, extra, duplicate or blank column; no rows imported | `import_worker.import_staged_batch`, using `csv_parser.find_header_problem` |
| `FAILED` | Bad UTF-8 or CSV quoting, a missing or corrupt staged file, or a database error during import (rolled back) | `import_worker.import_staged_batch` / `sales_importer.process_batch`; database errors are retried up to 3 attempts |

A `PROCESSING` batch older than 1 hour counts as stale and may be claimed again (see §11).

```mermaid
stateDiagram-v2
    [*] --> QUEUED: upload with a new file hash
    QUEUED --> PROCESSING: claimed by one worker (atomic UPDATE)
    PROCESSING --> COMPLETED: rows written, payload deleted
    PROCESSING --> REJECTED_HEADER: bad header, payload deleted
    PROCESSING --> FAILED: database error or unreadable file
    FAILED --> PROCESSING: retried while the payload is staged
    PROCESSING --> PROCESSING: reclaimed when stale (after 1 hour)
    COMPLETED --> [*]
    REJECTED_HEADER --> [*]

    note right of FAILED
        Database error: everything rolls back and the payload is kept,
        so the worker retries it (3 attempts in total).
        Unreadable file, or all 3 attempts used: the payload is deleted.
        Re-uploading the same file stages it again for one more try.
    end note
```

A worker only claims batches whose payload is still staged. Re-uploading a `COMPLETED` or `REJECTED_HEADER` file changes nothing and reports `ALREADY UPLOADED`.

### 4.4 Data model

Seven tables, defined in `backend/app/models.py` and created by the Alembic migrations in `backend/migrations/`. `alembic check` confirms the two match. Every column is `NOT NULL` unless marked "nullable".

```mermaid
erDiagram
    STORE ||--o{ TRANSACTION : "store_code"
    PRODUCT ||--o{ TRANSACTION : "sku"
    PRODUCT ||--o{ PRODUCT_ALIAS : "later names"
    IMPORT_BATCH ||--o{ TRANSACTION : "batch_id"
    IMPORT_BATCH ||--o{ IMPORT_ROW_ERROR : "rejected rows"
    IMPORT_BATCH ||--o| IMPORT_PAYLOAD : "gzipped file until imported"
    TRANSACTION |o--o{ TRANSACTION : "original_transaction_id (returns)"

    STORE {
        text code PK
        text name
        text city
    }
    PRODUCT {
        text sku PK
        text name "first name seen wins"
        timestamptz created_at
    }
    PRODUCT_ALIAS {
        bigint id PK
        text sku FK
        text alias_name "unique per sku"
        timestamptz first_seen_at
    }
    IMPORT_BATCH {
        uuid id PK
        text filename
        char file_sha256 UK "64 hex characters"
        timestamptz uploaded_at
        timestamptz started_at "nullable"
        timestamptz completed_at "nullable"
        text status "QUEUED to FAILED, see 4.3"
        int rows_total
        int rows_accepted
        int rows_rejected
        text failure_reason "nullable"
        int attempt_count
    }
    IMPORT_PAYLOAD {
        uuid batch_id PK, FK
        bytea csv_gzip "gzip of the original bytes"
        bigint received_size_bytes
        timestamptz created_at
    }
    TRANSACTION {
        text transaction_id PK
        text store_code FK
        timestamptz sold_at "stored in UTC"
        text sku FK
        text product_name "name as written in the file"
        int quantity "not 0, negative for a return"
        bigint unit_price_paise
        bigint discount_paise
        text payment_mode "CASH, UPI or CARD"
        text original_transaction_id FK "nullable, returns only"
        uuid batch_id FK
        bigint line_total_paise
        bool is_return
        timestamptz created_at
    }
    IMPORT_ROW_ERROR {
        bigint id PK
        uuid batch_id FK
        int row_number
        text column_name
        text message
        text transaction_id "nullable, not a foreign key"
    }
```

**Constraints in the schema** back up the service-layer rules:

| Kind | Constraints |
| --- | --- |
| Primary keys | `stores.code`, `products.sku`, `product_aliases.id`, `import_batches.id`, `import_payloads.batch_id` (at most one payload per batch), `import_row_errors.id`, `transactions.transaction_id` |
| Unique | `import_batches.file_sha256` (file idempotency, rule 1) · `product_aliases (sku, alias_name)` · `import_row_errors (batch_id, row_number)` |
| Foreign keys | `transactions.store_code → stores` · `transactions.sku → products` · `transactions.batch_id → import_batches` · `transactions.original_transaction_id → transactions` · `product_aliases.sku → products` · `import_row_errors.batch_id → import_batches` · `import_payloads.batch_id → import_batches` |
| CHECK on `transactions` | `quantity <> 0` · `unit_price_paise >= 0` · `discount_paise >= 0` · `payment_mode IN ('CASH', 'UPI', 'CARD')` · `is_return = (quantity < 0)` |
| CHECK elsewhere | `import_batches.status IN ('QUEUED', 'PROCESSING', 'COMPLETED', 'REJECTED_HEADER', 'FAILED')` · `import_row_errors.row_number >= 2` (row 1 is the header) · `import_payloads.received_size_bytes >= 0` |
| Defaults | Timestamps `uploaded_at`, `created_at` and `first_seen_at` default to `now()`; row counts and `attempt_count` default to 0 |

**Stored vs. derived:**
- **Derived, not stored:** the batch's error-report link (`/api/v1/imports/{id}/errors.csv`), the dashboard figures (computed with SQL on each request) and the money as rupees (computed from paise).
- **Stored on purpose:** `line_total_paise` and `is_return`, kept consistent with `quantity` by the CHECK above.
- **Not a foreign key:** `import_row_errors.transaction_id`, because a rejected row's transaction is never stored.
- **Indexes:** besides the primary keys and unique constraints, no secondary indexes exist yet (see §11).

### 4.5 Folder structure

| Path | Contents |
| --- | --- |
| `backend/app/` | Entry point (`main.py`: error handlers, middleware, health), config, DB session, models, Pydantic schemas, test clock, JSON logging |
| `backend/app/services/` | Business rules and the import pipeline (see 4.1). `csv_validator.py` only re-exports them for the CLI |
| `backend/api/` | HTTP routes for imports, transactions, analytics and stores; no business logic |
| `backend/migrations/` | Alembic migrations (the schema) |
| `backend/scripts/` | `migrate_and_seed.sh`, `seed.py` |
| `backend/seed_data/` | Seed CSVs: 6 stores, 5,003 sales (1 Sep – 20 Oct 2026) |
| `backend/tests/` | Unit, API and PostgreSQL concurrency tests |
| `frontend/vite-project/` | React UI: `src/pages/` (one file per page), `src/components/`, `src/api/client.ts` (the only API caller), `src/lib/format.ts` (₹ and IST formatting) |
| `Sample_Data/` | Sample files for every edge case in the problem statement (§10) |
| `.github/workflows/` | CI: ruff, pytest with coverage (including the PostgreSQL tests), frontend build |
| `docker-compose.yml` · `render.yaml` · `frontend/vite-project/vercel.json` | Local stack and hosting config |

### 4.6 Where the business rules live, and why

The problem requires every rule in the backend, so none exist only in the UI. Within the backend they are split by what they need:

| Needs | Module | Examples |
| --- | --- | --- |
| Only the row's text | `csv_parser.py`, `sales_rules.py` (pure functions, no HTTP, no DB) | Date format and "not in the future" (with an injected clock), quantity ≠ 0, discount ≤ gross, payment mode, paise conversion |
| Database state | `sales_importer.py`, against lookups loaded **once per file** | Store exists, duplicate `transaction_id`, cumulative return quantity vs. the original |
| Last line of defence | DB constraints in `models.py` and the migrations | Unique keys, non-negative money, `quantity <> 0` |

Keeping the pure rules free of I/O makes them easy to unit-test with boundary values (§6). It also lets the API, the CLI and the seed script share one implementation.

## 5. Running locally

**Prerequisites:** either of:

- Docker with Compose v2 (the one-command option), **or**
- Python 3.12+, Node 22.12+ (required by Vite 8) and PostgreSQL 16+.

### One command (Docker Compose)

```bash
docker compose up --build
```

| Service | Where | Notes |
| --- | --- | --- |
| UI | http://localhost:5173 | Open `localhost`, not `127.0.0.1`: CORS allows `http://localhost:5173` |
| API | http://localhost:8000 | Docs at `/docs` |
| Postgres | `localhost:5432` | User, password and database are all `kjbn` (local-only values) |

The backend runs `scripts/migrate_and_seed.sh` on every start, which is safe to repeat. Stop and wipe the data with `docker compose down -v`.

### Without Docker

```bash
# Backend
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # set DATABASE_URL (see table below)
sh scripts/migrate_and_seed.sh     # alembic upgrade head + seed; idempotent
uvicorn app.main:app --reload      # http://127.0.0.1:8000, docs at /docs

# UI (second terminal)
cd frontend/vite-project
cp .env.example .env
npm ci
npm run dev                        # http://localhost:5173
```

Quick database for this path: `docker compose up -d db`, then `DATABASE_URL=postgresql://kjbn:kjbn@localhost:5432/kjbn`.

### Environment variables

`backend/.env` (template `backend/.env.example`):

| Variable | Meaning | Default |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL URL. `postgresql://…` is fine; the psycopg 3 driver is added automatically. **Required.** | — |
| `APP_ENV` | Environment name | `development` |
| `MAX_UPLOAD_SIZE_BYTES` | Largest accepted CSV | `10485760` (10 MB) |
| `MAX_FILES_PER_UPLOAD` | Files per upload request | `10` |
| `UPLOAD_RATE_LIMIT_PER_MINUTE` | Upload requests per client IP per minute; 429 above it | `10` |
| `ALLOW_TEST_CLOCK` | Honour the `X-Test-Clock` header (§10). Never on in production | `false` |
| `CORS_ALLOWED_ORIGINS` | Comma-separated browser origins allowed to call the API (`*` is rejected) | `http://localhost:5173,http://127.0.0.1:5173` |

`frontend/vite-project/.env` (template `.env.example`): `VITE_API_BASE_URL`, the backend base URL. It is baked into the bundle at build time.

## 5a. Deployment

**Backend: Render**, from `render.yaml` (Blueprint):

- Root dir `backend`, Python 3.12.8
- Build: `pip install -r requirements.txt && alembic upgrade head`, so migrations run on every deploy
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health check path: `/health`

**Database: Neon PostgreSQL.**

**Frontend: Vercel**, deployed with the Vercel CLI from `frontend/vite-project`. The project link lives in the git-ignored `.vercel/` folder. `vercel.json` rewrites every path to `index.html`, so React Router pages survive a refresh.

**Manual steps done in provider dashboards, in order:**

1. **Neon:** create a project and copy the connection string.
2. **Render:** create the service from `render.yaml` and paste the Neon string as `DATABASE_URL`. It is `sync: false` in `render.yaml`, so it never lives in git.
3. **Render:** set `CORS_ALLOWED_ORIGINS` to the Vercel URL. `render.yaml` lists only the localhost origins, and the API rejects browser calls from any origin not listed.
4. **Vercel:** set `VITE_API_BASE_URL=https://kjbn-sales-backend.onrender.com`, then run `npx vercel --prod` from `frontend/vite-project`.
5. **Seed** the hosted database (below). The Render build only migrates.

**Redeploy after a code change:**
- **Backend:** push to `main`. Render rebuilds and re-runs the migrations.
- **UI:** run `npx vercel --prod` again.

**Re-seed the hosted database:** from `backend/`, with `DATABASE_URL` set to the Neon string, run:

```bash
sh scripts/migrate_and_seed.sh
```

The seed is idempotent:
- stores are upserted, so a second run reports "6 unchanged";
- the sales file has the same hash, so it is reported `ALREADY UPLOADED`.

To start from scratch, drop the tables or create a fresh Neon branch first.

## 6. Running tests and coverage

From `backend/`:

```bash
pytest                                                # all tests
pytest --cov=app --cov=api --cov-report=term-missing   # tests + coverage report
```

- **PostgreSQL concurrency tests** (`tests/test_concurrent_imports.py`, 2 tests) need real row locks. They run only when `TEST_POSTGRES_URL` is set, for example `TEST_POSTGRES_URL=postgresql://kjbn:kjbn@localhost:5432/kjbn pytest`. They use a throwaway schema and drop it afterwards.
- **Everything else** runs on in-memory SQLite and never touches a real database.
- **CI** (`.github/workflows/ci.yml`) runs all of it, including the PostgreSQL tests, on every push to `main`.

**Coverage:** the committed summary is in [`backend/coverage.txt`](backend/coverage.txt); **428 tests**.

| Scope | Coverage |
| --- | --- |
| Backend overall (`app/` + `api/`) | **99 %** (1,348 / 1,367 statements) |
| Business-rules layer: `sales_rules.py`, `sales_importer.py`, `csv_parser.py` | **100 %** (379 / 379) |

**What is covered:**

| Area | Examples |
| --- | --- |
| Every rule in §8, with a boundary test for each numeric rule | Discount exactly equal to gross vs. one paisa over · `sold_at` one microsecond after "now" · returns totalling exactly the original quantity vs. one more · the largest storable quantity and line amount vs. one more · well-formed but impossible dates (`2026-02-31`) |
| File handling | UTF-8 BOM, CRLF, missing/extra/duplicate/blank header columns, bad UTF-8, broken quoting |
| Duplicates | Within a file, against the database, and the whole file re-uploaded |
| Import pipeline | Two-pass return handling, chunk boundaries of the 1,000-row inserts, savepoint count not growing with the row count |
| Failure | Rollback when a write fails mid-batch; retries; concurrent claim of the same batch; a database outage returns 503 |
| API and logging | Every endpoint, including error shapes, rate limiting, CORS, request IDs, the test clock and the WARNING log for rejected rows; the seed script |

**Deliberately not covered (19 statements):**

| Where | Why |
| --- | --- |
| `import_worker.py` (95 %) | Database-down and unexpected-error branches of the command-line tool (`--queue`, direct import). The API paths doing the same work are covered |
| `analytics.py:51` | The PostgreSQL version of the IST-date SQL function. Unit tests run on SQLite, which has its own version; the PostgreSQL one runs in Docker and on the live instance |
| `api/routes.py:108` | The rate-limiter provider; tests inject their own limiter |
| Two one-line fallbacks | An unknown HTTP status name; the `__main__` entry point |

## 7. API overview

All paths are under the API base URL. Interactive docs: [`/docs`](https://kjbn-sales-backend.onrender.com/docs). Every error uses the single shape in §9.

| Method | Path | Purpose | Main error codes |
| --- | --- | --- | --- |
| GET | `/health` | Liveness | — |
| GET | `/health/db` | Database reachability | 503 |
| POST | `/api/v1/imports` | Upload 1–10 sales CSVs (multipart field `files`). Each file's extension and size are checked, then it is hashed and queued; rows import in the background. **202** if anything was queued. If nothing was queued: **400** if any file was invalid, otherwise **200** (every file was already uploaded). Per-file results in the body | 400 (too many files, no usable file) · 422 (no files) · 429 (rate limit, with `Retry-After`) · 503 |
| GET | `/api/v1/imports?limit=&offset=` | Batch history, newest first: counts, status, error-report link. `limit ≤ 100` | 422 · 503 |
| GET | `/api/v1/imports/{batch_id}/errors.csv` | Streamed CSV of a batch's rejected rows: `row_number, column_name, message, transaction_id` | 404 · 422 (bad UUID) · 503 |
| GET | `/api/v1/transactions` | Search: `store_code`, `sku` (exact), `date_from` / `date_to` (IST days, inclusive), `returns_only`, `page`, `limit ≤ 100`. Returns `total` and `pages` | 422 (bad values, `date_from` after `date_to`) · 503 |
| GET | `/api/v1/analytics/dashboard?date_from=&date_to=` | Revenue by IST day · revenue by store · top 10 products by net quantity · return rate by store · payment-mode split | 422 · 503 |
| GET | `/api/v1/stores` | Store catalog | 503 |
| POST | `/api/v1/stores` | Upsert the catalog from a `code,name,city` CSV (shares the upload rate limit) | 400 · 422 · 429 · 503 |

**Responses:**
- **Money** is `{"paise": <int>, "rupees": "<string>"}`.
- **Timestamps** are ISO-8601 in UTC; the UI renders them in IST.
- **Lists:** batch history and transaction search are paginated (`limit ≤ 100`). The store list returns the whole catalog.

## 8. Business rules and assumptions

### 8.1 Rule → enforcement → test

| # | Rule (Problem 5) | Enforced in | Tests (`backend/tests/`) |
| --- | --- | --- | --- |
| 1a | Re-uploading an already-processed file (same hash) is reported as a duplicate and changes nothing | `import_worker.enqueue_upload` / `register_import_batch`: UNIQUE `file_sha256` | `test_csv_validator.py::test_same_hash_returns_existing_batch_without_creating_another` · `test_api_imports.py::test_reupload_returns_200_with_the_existing_batch` |
| 1b | A different file with already-seen `transaction_id`s accepts the new rows and rejects the repeats individually | `sales_importer.accept_row` (`DUPLICATE_IN_FILE`, `DUPLICATE_IN_DATABASE`); conflict re-check in `import_rows` | `test_csv_validator.py::test_id_already_in_database_is_rejected` · `::test_duplicate_id_in_file_keeps_first_and_rejects_later` · `test_concurrent_imports.py::test_overlapping_id_rejects_only_the_duplicate_and_keeps_new_rows` |
| 2 | Row-level validation (row, column, message); only a bad header rejects the file; error report as CSV | `csv_parser.validate_row`, `find_header_problem`; `api/routes.download_import_errors` | `test_csv_validator.py::test_processing_continues_after_invalid_rows` · `::test_missing_column_is_rejected` · `test_api_imports.py::test_error_csv_lists_rejected_rows` |
| 3 | All columns present; store exists; `sold_at` is `YYYY-MM-DD HH:MM:SS` IST and not in the future; quantity is a non-zero integer; `unit_price ≥ 0`; `0 ≤ discount ≤ quantity × unit_price`; payment mode in {CASH, UPI, CARD} | `csv_parser.validate_row` (presence); `sales_importer.accept_row` (`UNKNOWN_STORE`); `sales_rules.parse_sold_at`, `parse_quantity`, `parse_paise`, `validate_sale`, `validate_amounts` | `test_sales_rules.py` (`test_zero_quantity_is_rejected`, `test_discount_just_over_gross_is_rejected`, `test_timestamp_just_after_now_is_rejected`, `test_well_formed_but_impossible_sold_at_is_rejected`, `test_unknown_payment_modes_are_rejected`, …) · `test_csv_validator.py::test_rule_violations_are_rejected_with_column_and_message` · `::test_boundary_values_are_accepted` |
| 4 | A return (negative quantity) references an existing original from the same store and SKU; total returned ≤ the original quantity across all returns | `sales_rules.validate_return`; cumulative totals and `FOR UPDATE` locks on originals in `sales_importer.load_import_state` | `test_sales_rules.py::test_returns_beyond_original_quantity_are_rejected` and the other `test_return_*` tests · `test_csv_validator.py::test_cumulative_returns_across_files_cannot_exceed_sale` · `test_concurrent_imports.py::test_concurrent_returns_of_last_unit_only_one_succeeds` |
| 5 | Line total = quantity × unit\_price − discount (negative for returns); money as integer paise | `sales_rules.line_total_paise`; BIGINT paise columns in `models.py` | `test_csv_validator.py::test_valid_sale_is_saved_with_converted_values` · `test_api_transactions.py::test_items_show_money_in_paise_and_rupees` |
| 6 | The same file imported twice concurrently: only one wins; 20k rows without timing out the request | UNIQUE hash + atomic claim (`import_worker.claim_batch`); 202 + background task; in-memory plan and 1,000-row inserts (`sales_importer.plan_import`, `write_plan`) | `test_csv_validator.py::test_batch_already_processing_is_not_imported_again` · `::test_rows_and_errors_are_inserted_in_chunks` · `::test_savepoints_do_not_grow_with_the_number_of_rows` |

**Worked example (rule 5):** `2 × 540.00 − 40.00` is computed as `2 × 54000 − 4000 = 104000` paise and returned as `{"paise": 104000, "rupees": "1040.00"}`. The matching return, `−1 × 540.00 − 0`, gives `−54000`. No floats are involved at any step: `parse_paise` turns the text `540.00` into `54000` with exact decimal arithmetic.

### 8.2 How one row is checked

Each row goes through the checks below in this order and stops at the first failure. That failure becomes one row error (row number, column, message) in `import_row_errors`, which feeds the batch's counts and its error CSV. Each rule code is reported against one column (`COLUMN_BY_CODE` in `sales_rules.py`). A row with the wrong number of fields is reported against the column `__row__`.

```mermaid
flowchart TD
    IN["CSV row, values trimmed"] --> S1["1 · Shape (csv_parser.validate_row)<br/>exactly 10 fields, else a whole-row error<br/>every required value present, else the first missing column"]
    S1 --> S2["2 · Order (sales_importer.processing_order)<br/>pass 1: all sales · pass 2: all returns<br/>each pass oldest sold_at first"]
    S2 --> S3["3 · Duplicates (sales_importer.accept_row)<br/>DUPLICATE_IN_FILE · DUPLICATE_IN_DATABASE"]
    S3 --> S4["4 · Formats (sales_rules.parse_sale_row)<br/>INVALID_SOLD_AT · INVALID_QUANTITY<br/>INVALID_UNIT_PRICE · INVALID_DISCOUNT"]
    S4 --> S5["5 · Store exists<br/>UNKNOWN_STORE"]
    S5 --> S6["6 · Every row (sales_rules.validate_sale)<br/>INVALID_QUANTITY (zero) · INVALID_PAYMENT_MODE · FUTURE_TIMESTAMP<br/>INVALID_UNIT_PRICE / INVALID_DISCOUNT (negative)<br/>AMOUNT_TOO_LARGE · DISCOUNT_EXCEEDS_GROSS"]
    S6 --> Q{"Positive quantity?"}
    Q -- "yes: a sale" --> S7["7a · Sale<br/>SALE_HAS_ORIGINAL"]
    Q -- "no: a return" --> S8["7b · Return (sales_rules.validate_return)<br/>RETURN_DISCOUNT_NOT_ZERO · RETURN_ORIGINAL_REQUIRED<br/>RETURN_ORIGINAL_NOT_FOUND · RETURN_ORIGINAL_NOT_A_SALE<br/>RETURN_BEFORE_SALE · RETURN_STORE_MISMATCH<br/>RETURN_SKU_MISMATCH · RETURN_QUANTITY_EXCEEDS_ORIGINAL"]
    S7 --> OK["Accepted: added to the insert plan.<br/>Later rows see it as a duplicate ID,<br/>a return's original, or in a return total"]
    S8 --> OK
    S1 & S3 & S4 & S5 & S6 & S7 & S8 -. "first failure" .-> REJ["Rejected: one row error<br/>row number · column · message"]
```

Because sales are planned before returns, a return can refer to a sale listed further down the same file.

### 8.3 Concurrent imports

Two imports that return the last unit of the same sale at the same time are serialised by a row lock. `sales_importer.load_import_state` locks the original sales with `SELECT … FOR UPDATE`, and only then reads how much of each was already returned:

```mermaid
sequenceDiagram
    participant A as Import A (return R1 of sale T1)
    participant DB as PostgreSQL
    participant B as Import B (return R2 of sale T1)

    Note over DB: T1 sold 1 unit, none returned yet
    A->>DB: SELECT T1 FOR UPDATE (lock taken)
    A->>DB: units already returned against T1 = 0
    B->>DB: SELECT T1 FOR UPDATE
    Note over B,DB: B waits, because A holds the lock on T1
    A->>A: R1 returns 1 of 1, accepted
    A->>DB: INSERT R1, COMMIT (lock released)
    DB-->>B: lock granted
    B->>DB: units already returned against T1 = 1
    B->>B: R2 would make 2 of 1, rejected (quantity)
    B->>DB: INSERT the row error, COMMIT
```

- **Batch outcome:** both batches complete. B reports its row as `RETURN_QUANTITY_EXCEEDS_ORIGINAL`, so returns can never exceed the sale.
- **No deadlocks:** locks are always taken in `transaction_id` order, so two imports can't lock in opposite orders.
- **The other race:** two files importing the same new `transaction_id` at once. Inserts use `ON CONFLICT DO NOTHING`; if a row was skipped, that import rolls back to its savepoint, re-reads the conflicting keys and plans again. The repeated ID then becomes `DUPLICATE_IN_DATABASE` while its other rows still import.
- **Tests:** both races are covered against real PostgreSQL, in `test_concurrent_imports.py::test_concurrent_returns_of_last_unit_only_one_succeeds` and `::test_overlapping_id_rejects_only_the_duplicate_and_keeps_new_rows`.

### 8.4 Edge cases from the problem statement

| Edge case | Result | Test / sample |
| --- | --- | --- |
| Sample file uploaded twice | The second upload reports `ALREADY UPLOADED` and nothing changes | `test_api_imports.py::test_reupload_returns_200_with_the_existing_batch` |
| Same rows in two files with different names | Different hash, so imported; each repeated row is rejected as `DUPLICATE_IN_DATABASE` | `test_csv_validator.py::test_id_already_in_database_is_rejected` · `sample_duplicate_ids_renamed.csv` |
| Return before its original in the same file | Accepted: returns are validated in a second pass | `test_csv_validator.py::test_return_listed_before_its_sale_is_accepted` |
| Return exceeding the original across two files | Rejected with a `quantity` error | `test_csv_validator.py::test_cumulative_returns_across_files_cannot_exceed_sale` |
| UTF-8 BOM · CRLF line endings | Accepted | `::test_utf8_with_bom_passes_and_bom_is_not_part_of_first_header` · `::test_lf_and_crlf_line_endings_pass` |
| 20k-row file | The upload returns 202 immediately; `Sample_Data/sample_21k.csv` imports in seconds | `::test_rows_and_errors_are_inserted_in_chunks` |
| Quantity 0 | Rejected | `test_sales_rules.py::test_zero_quantity_is_rejected` |
| Date range with no data | Zeros and empty lists, no errors | `test_api_dashboard.py::test_range_without_any_sales_returns_zeros_without_errors` |

### 8.5 Assumptions (ambiguities I resolved)

1. **Time zone.** `sold_at` in the CSV is IST. It is stored in UTC and shown in IST. Dashboard and search ranges are IST calendar days, inclusive at both ends.
2. **"Not in the future"** is judged against the server clock at import time (or the `X-Test-Clock` time). A `sold_at` exactly equal to "now" is accepted.
3. **Money format.** Rupees with at most 2 decimals: `540`, `540.5` and `540.00` are fine; `540.001` is rejected. Amounts are converted to paise without floats.
4. **Header.** Names must match exactly (case and spelling), in any order. A missing, extra, duplicate or blank column rejects the whole file (`REJECTED_HEADER`). Bad UTF-8 or malformed quoting also rejects the whole file (`FAILED`), because its rows can't be read reliably.
5. **`original_transaction_id`** is optional and only allowed on returns. A sale carrying one is rejected (`SALE_HAS_ORIGINAL`).
6. **Stores are never created by a sales import.** An unknown `store_code` rejects the row. Stores come from `POST /api/v1/stores` or the seed.
7. **Return discount** must be 0: the refund is the line total of the units returned.
8. **Return timing.** A return must be sold strictly after its original; the same second is rejected.
9. **The original** must be a sale, not another return.
10. **Duplicate IDs within one file** are decided in `sold_at` order: the earliest copy is kept and later copies are rejected. If the first copy was itself rejected, a later valid copy can be accepted.
11. **File identity** is the SHA-256 of the bytes. The same rows saved with different line endings are a different file; their rows are then rejected individually as duplicates (rule 1b).
12. **Retries.** A database error during an import is retried automatically, up to 3 attempts in total. Re-uploading a file whose batch ended `FAILED` queues it again. Re-uploading any other already-processed file, including a header-rejected one, only reports `ALREADY UPLOADED`.
13. **Product names.** The first name seen for a SKU wins. Each different later name is stored once as an alias. A product row is only created by an accepted row.
14. **Revenue is net:** the sum of line totals, so returns subtract.
15. **Return rate** is units returned ÷ units sold for each store in the range; 0 when nothing was sold.
16. **Top 10 products** are ranked by net quantity (sold − returned). A product with only returns isn't listed.
17. **Payment-mode split** covers sales only (how customers paid), so shares are never negative and add up to 100 %.
18. **No date range** on the dashboard means everything up to today (IST). The UI defaults to "This month".
19. **Transaction search** filters (`sku`, `store_code`) are exact matches, not substrings.
20. **Spaces and case in values.** Every value is trimmed of surrounding spaces before validation (header names are not trimmed, see 4). Payment mode is case-insensitive: `upi` is accepted and stored as `UPI`.

## 9. Error handling and logging

### 9.1 One error shape, everywhere

```json
{
  "error": {
    "status": 422,
    "code": "validation_error",
    "message": "Some request values are missing or invalid.",
    "request_id": "8d8d9f5b-…",
    "details": [{"field": "query.limit", "message": "…", "type": "…"}]
  }
}
```

`details` appears only on validation errors and never echoes the submitted values. Clients never see a stack trace.

### 9.2 How each kind of failure travels

| Kind | Origin | What the client gets | What is logged |
| --- | --- | --- | --- |
| Malformed request | Body isn't JSON; broken multipart | **400** `malformed_json` / `bad_request` | INFO request line |
| Validation | Wrong types, out-of-range values, missing fields (FastAPI/Pydantic) | **422** `validation_error` with `details` | INFO request line |
| Request-level rules | Too many files, no usable file, `date_from` after `date_to`, unknown batch, rate limit | **400 / 404 / 422 / 429** with a specific message (429 carries `Retry-After`) | INFO request line |
| Row-level business rule | `SaleRuleViolation` with a code (e.g. `RETURN_QUANTITY_EXCEEDS_ORIGINAL`) mapped to a column | **Not an HTTP error.** The row is stored in `import_row_errors`, counted on the batch and listed in the error CSV; the rest of the file imports | WARNING: one line per batch, with per-column counts and examples |
| Database unavailable | Connection refused or lost, or the pool exhausted (`OperationalError`, `InterfaceError`, pool `TimeoutError`) | **503** ("Database unavailable…") with no internals. Uploads and the store import catch it themselves; every other endpoint gets it from the request middleware | ERROR with traceback and request ID. A failed import rolls back entirely, the batch becomes `FAILED`, and it is retried up to 3 times |
| Unexpected exception | Anything else, including other database errors (a bug, not an outage) | **500** "Something went wrong on our side", with the `request_id` | ERROR with traceback |

**Data consistency:** each batch imports in one transaction. A failure at any point, even after thousands of rows were inserted, leaves nothing behind.

**UI:** errors show the API's message and request ID, and failed data loads offer a Retry button. Every page has loading and empty states.

### 9.3 Logging

Structured JSON, one object per line, on stdout. Common fields: `timestamp`, `level`, `logger`, `message`, `request_id`, `path`.

| Level | Event | Extra fields |
| --- | --- | --- |
| INFO | Each completed request | `method`, `status_code`, `duration_ms` |
| INFO | Each imported batch | `batch_id`, `file_name`, `rows_total`, `rows_accepted`, `rows_rejected` |
| WARNING | Rows rejected by validation or rules (one line per batch) | The batch fields above, plus `rejected_by_column` (e.g. `{"quantity": 12, "store_code": 3}`) and `examples` (the first 5 rejected rows: row, column, transaction ID, reason) |
| WARNING | A batch rejected for its header or encoding, or a failed import attempt (retried or given up) | `batch_id`, `file_name`, `status`; `attempt`, `max_attempts`, `will_retry` |
| ERROR with traceback | Unexpected exceptions, database outages, failed imports | `batch_id`, `file_name` where relevant |

- **Correlation:** the request ID comes from `X-Request-ID` if it is a safe value; otherwise one is generated. It is echoed in the response header and the error body, and attached to every log line of that request, **including the background import it starts**.
- **Never logged:** request and file bodies. The rejection warning quotes at most 5 rows' reasons, and the sales data contains no personal information.
- **CLI and seed script:** they also print a plain-text summary for the person running them. The API's background imports turn that off, so the server's output is JSON only.
- **Where to read the logs:** hosted, in the Render dashboard → `kjbn-sales-backend` → **Logs**. Locally, in the uvicorn terminal or with `docker compose logs backend`.

## 10. Reviewer aids

### 10.1 Review in ten minutes

On the hosted instance `sample_sales.csv` has already been imported, so step 1 there shows `ALREADY UPLOADED`, which is the point of step 2. On a fresh local stack, every step behaves as listed.

1. Open the UI → **Data Ingestion** → drop `Sample_Data/sample_sales.csv` and click **Upload Files**. The result shows `Queued`, and the **Batch History Ledger** refreshes itself until the batch is `Completed` (48 accepted).
2. Drop the **same file again**: `ALREADY UPLOADED`, and the counts don't change.
3. Drop `sample_duplicate_ids_renamed.csv`: 2 accepted, 48 rejected as already imported. Download its `errors.csv`.
4. Drop `sample_returns_part1.csv` (8 accepted), then `sample_returns_part2.csv`: 4 accepted and 4 rejected, namely an over-return (`quantity`), a store mismatch, an SKU mismatch and an unknown original.
5. Drop `sample_bad_header.csv`: the whole file is `Header Rejected`. Then `sample_bad_rows.csv`: 6 accepted, 22 rejected with row-level reasons.
6. Drop `sample_21k.csv`: the upload returns at once, and the batch completes in the history with 20,672 accepted and 328 rejected.
7. **Executive Analytics** (dashboard): pick a range in Sep–Oct 2026 (seed data), then a range with no data.
8. **Transaction Explorer:** filter by store, SKU, date range and returns only. The filters stay in the URL, so a refresh keeps them.

### 10.2 Hitting the API directly

```bash
API=https://kjbn-sales-backend.onrender.com

curl -s $API/health/db
curl -s -F "files=@Sample_Data/sample_sales.csv" $API/api/v1/imports | jq
curl -s "$API/api/v1/imports?limit=5" | jq
curl -s "$API/api/v1/imports/<batch_id>/errors.csv"
curl -s "$API/api/v1/transactions?store_code=ST03&returns_only=true&limit=10" | jq
curl -s "$API/api/v1/analytics/dashboard?date_from=2026-10-01&date_to=2026-10-20" | jq
# malformed input → 422 with details, never 500
curl -s "$API/api/v1/transactions?limit=abc" | jq
```

### 10.3 Sample files (`Sample_Data/`)

| File | Shows |
| --- | --- |
| `sample_sales.csv` | 48 valid sales |
| `sample_returns_part1.csv`, `sample_returns_part2.csv` | Returns, including cumulative returns across two files and mismatched store, SKU or original |
| `sample_return_before_original.csv` | A return listed before its sale |
| `sample_duplicate_ids_renamed.csv` | `sample_sales.csv`'s 48 rows plus 2 new ones under a different name: only the 2 new rows are accepted |
| `sample_bad_rows.csv` | Row-level errors (unknown store, wrong date format, …) |
| `sample_bad_header.csv` | Whole-file rejection (missing `payment_mode` column) |
| `sample_bom_crlf.csv` | BOM and CRLF line endings |
| `sample_edge_cases.csv` | Boundaries and formatting: 11 accepted, 1 rejected. Accepted: discount equal to gross (line total 0), zero price, `23:59:59` and `00:00:00`, quoted commas, padded values, lowercase `upi`, prices without decimals (`25`) or with one (`399.5`); two new names for `SKU-114` become aliases. Rejected: a return with a non-zero discount |
| `sample_21k.csv` | 21,000 rows |
| `stores_100.csv` | A 100-store catalog for `POST /api/v1/stores` |

### 10.4 Test clock

Send `X-Test-Clock: 2026-10-17T09:00:00Z` (any ISO-8601 time; no offset means UTC). That request then treats the value as "now" for:
- the future-date check on imports;
- batch timestamps;
- "today" on the dashboard and in transaction search.

An invalid value returns 400. The header is honoured only when `ALLOW_TEST_CLOCK=true`. It is `false` on the hosted instance (`render.yaml`), so use it locally: set it in `backend/.env`, or under `environment:` in `docker-compose.yml`.

### 10.5 Other aids

- **Re-seed:** see §5a. The seed data spans 1 Sep – 20 Oct 2026, so "This month" and "Last 7 days" show data.
- **Command-line import**, from `backend/`: `python -m app.services.csv_validator path/to/file.csv` prints per-row errors and a summary. Add `--queue` to stage only, or `--worker --once` to process staged batches.
- **OpenAPI:** `/docs` lists every endpoint, parameter and error response.

## 11. Known limitations

None of these breaks a Core rule; they are trade-offs or gaps worth knowing about.

| Area | Limitation |
| --- | --- |
| Interrupted imports | Imports run as a FastAPI background task in the web process, not in a separate worker. If the process restarts mid-import, nothing is lost (the transaction rolls back), but the batch stays `PROCESSING` until it counts as stale (1 hour). After that it is picked up by the next upload that queues any file (each upload's background task drains every claimable batch), or by `python -m app.services.csv_validator --worker --once`. Re-uploading the stuck file itself only reports `ALREADY UPLOADED` |
| Retry budget | Attempts never reset: a batch that failed all 3 attempts gets only one more try when re-uploaded |
| Rate limiting | In memory, per process: it resets on restart and isn't shared across instances |
| Upload result timing | The upload response shows each file's status (queued, already uploaded, invalid) immediately. Accepted/rejected counts and the error-report link appear in the batch history when the background import finishes (seconds; the page refreshes while a batch is in flight) |
| Memory | Uploads are read whole into memory (capped at 10 MB per file, 10 files per request) and stored gzipped until imported; they are not streamed |
| Indexes | `transactions` has no secondary indexes on `sold_at`, `store_code`, `original_transaction_id` or `batch_id`. At ~25k rows queries are fast but scan the table; larger volumes would need them |
| Undo | A completed batch can't be deleted or rolled back from the UI or API |
| Exact-match names | Product names are compared exactly after trimming, so `Basmati Rice 5kg` and `Basmati Rice 5 kg` give a name plus an alias rather than one product |
| Batch history filter | The filter box searches only the page of batches currently loaded, not all batches |
| Test clock | Off on the hosted instance; use it locally |
| Free tiers | The API sleeps when idle (first request 30–60 s) and Neon suspends; both wake automatically |
| Security | No authentication: anyone with the URL can upload files and import store catalogs |
| Stretch | None attempted: no scheduled inbox/S3 fetch, no anomaly flag, no dashboard CSV export |

## 12. AI and third-party usage

- **Claude Code (Anthropic, Claude Opus):**
  - **Backend:** implementing and refactoring the backend to my specifications: CSV validation, sales rules, the import pipeline (two-phase in-memory validation, chunked inserts), API endpoints, error handling, logging, and the tests.
  - **Frontend:** building the React UI from the design files.
  - **Infrastructure:** the GitHub Actions workflow, Dockerfiles and `docker-compose.yml`.
  - **Explanation and review:** analysing the codebase against the assignment and explaining code.
  - **Drafting:** this README and some of my prompts.
  - **Guidance file:** `CLAUDE.md` at the repo root holds my engineering rules for it.
- **Claude (Anthropic, Fable 5.1 model):** restructuring this README and drafting its Mermaid diagrams.
- **Google Stitch:** generated the UI design references (layout, colours, typography) that the frontend implements.
- **Libraries:** as listed in `backend/requirements.txt` and `frontend/vite-project/package.json`. No templates or copied project code.

**Prompt log:** [PROMPTS.md](PROMPTS.md): every prompt in order, with the time, the tool, and what I did with each answer (used as-is, edited or rejected).
