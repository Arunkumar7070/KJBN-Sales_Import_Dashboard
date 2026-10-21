# SUBMISSION — Problem 5: Sales Import & Dashboard

## A. What I built

I built a FastAPI + PostgreSQL service and a React dashboard: head office uploads the stores' daily sales CSVs, bad rows are rejected one at a time with a downloadable error report, and managers see revenue, returns and payment trends. Every Core API feature is complete — idempotent upload by SHA-256 hash, batch history, error-report CSV, transaction search (store, IST date range, SKU, returns only) and all five dashboard aggregates over a date range. Every Core UI flow is complete — drag-and-drop upload with an accepted/rejected summary and error-report link, batch history that polls until imports finish, a dashboard with a date filter, three charts and a top-10 products table, and a transaction explorer. Every business rule is enforced in the backend and repeated as a database constraint, and all nine listed edge cases pass (a 21k-row file imports in about 2 s locally). Partial: upload results arrive in two steps (202 immediately, counts once the background import finishes), phone widths are not fully polished, and no Stretch item was attempted.

## B. Three design decisions

**1. The database is the queue and the lock.**

- **Decision:** upload stores the gzipped file, inserts a `QUEUED` batch (UNIQUE on the file hash) and returns 202; a background worker claims it with one conditional `UPDATE … WHERE status is claimable`.
- **Alternative considered:** a synchronous import, or Redis + Celery.
- **Why I chose this:** it meets both the 20k-row rule and "only one wins" with no extra infrastructure, and the uniqueness race is settled by PostgreSQL, not application code.
- **What it cost me:** the worker shares the API process, so a crash mid-import leaves the batch `PROCESSING` until the 1-hour stale timeout.

**2. Validate the whole file in memory, then write in chunks.**

- **Decision:** lock the referenced originals with `FOR UPDATE`, plan every row in memory (sales first, then returns), then insert accepted rows in 1,000-row `INSERT … ON CONFLICT DO NOTHING` chunks; if a chunk collides, re-plan once.
- **Alternative considered:** one savepoint per row.
- **Why I chose this:** 88 statements instead of 62,149 for the 21k-row sample, with identical results.
- **What it cost me:** a harder retry path, and a whole file's rows held in memory (bounded by the 10 MB cap).

**3. Pure rules, duplicated constraints, derived analytics.**

- **Decision:** `sales_rules.py` and `csv_parser.py` do no I/O. Every rule is repeated as a named CHECK or UNIQUE constraint. Dashboard numbers are SQL aggregates computed per request.
- **Alternative considered:** cached daily aggregates updated on each import.
- **Why I chose this:** rules are unit-testable without a database, the schema catches anything the service misses, and nothing can go stale.
- **What it cost me:** every dashboard load scans `transactions`, and there are no secondary indexes yet.

## C. The hardest part

Making a 20k-row import fast without losing row-level isolation.

My M2 design locked the originals with `FOR UPDATE` (`e24c8c6`) and gave every row its own savepoint (`84b7271`), so one bad row could never roll back the file; the background job (`d4a9b57`) kept the HTTP request fast. It was correct. Then I timed the 21,000-row sample against Neon: over ten minutes, because each row cost several network round trips.

In `439e5b9` I split the import into two phases: validate every row in memory against a pre-locked snapshot of originals and already-returned quantities, then insert accepted rows in 1,000-row chunks. Statements fell from 62,149 to 88 and local time from 7.3 s to 2.0 s, with identical table checksums.

The PostgreSQL concurrency tests then caught a second mistake: psycopg reports `rowcount = -1` for multi-row inserts, so every chunk looked like a conflict. `preserve_rowcount=True` fixed it.

## D. How I handle failure

Every HTTP error uses one envelope: `{"error": {status, code, message, request_id, details?}}`. The `request_id` is echoed in the response header and attached to every log line of that request, including the background import it starts.

| Failure | What the user sees | What is logged |
| --- | --- | --- |
| **Bad request** — wrong type, missing field, broken multipart, `date_from` after `date_to` | `400` / `422` with field names in `details`; submitted values are never echoed | `INFO` access line: status, duration, `request_id` |
| **Rule violation** — a bad row | Only that row is rejected; the file continues. The batch shows counts and the error CSV gives row, column, message. A bad header marks the whole file `REJECTED_HEADER` | `WARNING` with `batch_id`, `file_name`, rejections by column, first 5 examples (no row contents) |
| **Database error** | API: `503 Database unavailable`. Import: the transaction rolls back (no partial rows), batch goes `FAILED`, retried up to 3 times | `ERROR` with traceback, `batch_id`, `request_id` |
| **Unexpected exception** | Sanitised `500` with the `request_id` to quote; no stack trace | `ERROR` with full traceback and `request_id` |

## E. Milestone log

| Milestone | Date completed | Commit |
| --- | --- | --- |
| M0 — Scaffold | 2026-10-17 | `863a0f7` |
| M1 — Domain model | 2026-10-17 | `b0e9e93` |
| M2 — Business rules | 2026-10-19 | `d4a9b57` |
| M3 — API | 2026-10-20 | `5b79d20` |
| M4 — UI | 2026-10-21 | `3c44b9f` |
| M5 — Deployment | 2026-10-21 | `e842d3b` |
| M6 — Documentation | 2026-10-21 | `4a7d87e` · tag `v1.0` |

## F. If I had two more days

1. **Add indexes on `transactions`** (`sold_at`, `store_code`, `sku`, `batch_id`). First because it is cheap and every import makes the dashboard and search scans slower.
2. **Move the import worker into its own process, with Redis as the job queue.** A restart mid-import is the main way a batch gets stuck today; Redis lets the API hand off a batch and the worker pick it up independently, and the existing atomic claim on `import_batches` stays as the safety net.
3. **Back the upload rate limiter with Redis.** The in-memory one resets on restart and would not work across instances; once Redis is there for the queue, a shared counter is a small change.
4. **Do a phone-width pass on every page.** It is a rubric item, and the UI was designed at laptop width.
5. **Add the >40 % daily-revenue anomaly flag.** The first Stretch item, last because Core hardening matters more.

## G. Self-assessment

| Criterion | Score | Justification |
| --- | --- | --- |
| Business logic correctness | 5 | Every rule is enforced in the backend and repeated as a DB constraint; paise and IST handled precisely; concurrent imports tested on real PostgreSQL. |
| Code clarity and structure | 4 | Routes → services → models layering enforced by ruff, but `import_worker.py` (\~520 lines) mixes CLI output with worker logic. |
| Error and exception handling | 5 | Each failure type has its own status (400/422/429/503/500) in one envelope; failed imports roll back and retry; each path is tested. |
| Logging and observability | 4 | JSON logs carry `request_id` into background jobs and log rule violations at WARNING with context; logs only in the Render dashboard, no metrics. |
| Data modelling and persistence | 4 | Named CHECKs, UNIQUE hash and IDs, BIGINT paise, Alembic migrations, a deliberate derived-vs-stored choice; no secondary indexes yet. |
| API design | 4 | Versioned `/api/v1` with resource paths, pagination, filters and OpenAPI; the store list is unpaginated (the catalog is bounded). |
| UI usability | 4 | Core flows have loading, empty and error states and live batch polling, but phone widths are not verified. |
| Testing | 5 | 428 tests, a boundary test for every numeric rule, PostgreSQL concurrency tests, 100 % on the rules layer and 99 % overall, committed as `coverage.txt`. |
| Deployment and operability | 4 | Dockerfiles, compose, `render.yaml`, `vercel.json`, migrate-and-seed script and CI all present; some provider-dashboard steps are manual; the free tier cold-starts. |
| Git hygiene | 4 | Milestone commits and steady progress, but some subjects break the format (`feat : Added seed script.`, `Added sample data files`), the seed script landed in M5 instead of M1, and two commits add the same table. |
| Documentation and write-up | 4 | README follows Section 6 with diagrams, 19 assumptions and honest limitations; this write-up cites real commits. |
