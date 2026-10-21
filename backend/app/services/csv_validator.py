"""Public façade re-exporting CSV parsing, sales ingestion, and worker functions.

This file serves as a single, backwards-compatible import entry point so that
existing API routes and automated pytest tests continue to run without changes.

Monkeypatching a name here does not change the code that uses it: patch the
module that owns it (csv_parser, sales_importer or import_worker) instead.
"""

# Every import below is a re-export; keep `ruff check --fix` from deleting them.
# ruff: noqa: F401

import sys

from app.database import SessionLocal

# 1. Pure File and CSV Parsing (from csv_parser.py)
from app.services.csv_parser import (
    OPTIONAL_FIELDS,
    REQUIRED_HEADERS,
    WHOLE_ROW,
    CsvFileError,
    CsvHeaderError,
    CsvRow,
    FileCheckResult,
    RowError,
    check_csv_content,
    check_csv_file,
    check_name_and_size,
    decode_utf8,
    describe_unexpected,
    find_header_problem,
    parse_records,
    read_file,
    validate_row,
    validate_rows,
)

# 3. Background Worker, Queue & CLI Runner (from import_worker.py)
from app.services.import_worker import (
    ALREADY_UPLOADED,
    ERROR,
    FAILED,
    GZIP_LEVEL,
    IMPORTED,
    INVALID,
    MAX_IMPORT_ATTEMPTS,
    QUEUED,
    STALE_PROCESSING_AFTER,
    SUMMARY_ORDER,
    WORKER_POLL_INTERVAL_SECONDS,
    BatchRegistration,
    FileOutcome,
    claim_batch,
    claim_next_batch,
    enqueue_upload,
    finish_without_import,
    import_file,
    import_staged_batch,
    is_claimable,
    main,
    print_summary,
    queue_file,
    record_batch,
    register_import_batch,
    report_completed,
    run_next_batch,
    run_worker,
    schedule_retry_or_give_up,
    utc_now,
)

# 2. Database Lookups, Validation & Chunked Inserts (from sales_importer.py)
from app.services.sales_importer import (
    CONFLICT_SKIPPING_INSERTS,
    INSERT_CHUNK_SIZE,
    LOOKUP_CHUNK_SIZE,
    MAX_WRITE_ATTEMPTS,
    ImportConflictError,
    ImportPlan,
    ImportState,
    accept_row,
    chronological_key,
    chunked,
    fetch_where_in,
    import_rows,
    insert_in_chunks,
    insert_new_rows,
    is_return_row,
    load_import_state,
    plan_import,
    plan_row,
    process_batch,
    processing_order,
    refresh_conflicting_lookups,
    sale_row_from_database,
    table_of,
    write_plan,
)

if __name__ == "__main__":
    sys.exit(main())
