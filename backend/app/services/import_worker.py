"""Background worker, staging queue, and CLI runner.

This module provides the asynchronous engine for CSV imports:
- Files are gzipped and staged into `import_payloads` with a QUEUED status.
- Workers atomically claim batches using `claim_batch` (preventing double runs).
- Stale batches whose workers crashed are automatically re-queued.
- Can be invoked from the CLI with `python -m app.services.import_worker`.
"""

import argparse
import gzip
import hashlib
import logging
import sys
import time
import uuid
from collections import Counter
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.database import SessionLocal
from app.models import ImportBatch, ImportPayload
from app.services.csv_parser import (
    CsvFileError,
    FileCheckResult,
    RowError,
    check_csv_content,
    check_csv_file,
    check_name_and_size,
    read_file,
)
from app.services.sales_importer import process_batch

logger = logging.getLogger(__name__)

# Plain-text progress for people running the CLI or the seed script. The API's
# background imports switch it off (without_echo): there the structured logs
# written by process_batch and the functions below are the record.
_echo_enabled: ContextVar[bool] = ContextVar("echo_enabled", default=True)


def echo(message: str, *, to_stderr: bool = False) -> None:
    if _echo_enabled.get():
        print(message, file=sys.stderr if to_stderr else sys.stdout)


@contextmanager
def without_echo() -> Iterator[None]:
    token = _echo_enabled.set(False)
    try:
        yield
    finally:
        _echo_enabled.reset(token)

# Settings for worker retries and recovery
MAX_IMPORT_ATTEMPTS = 3
STALE_PROCESSING_AFTER = timedelta(hours=1)
WORKER_POLL_INTERVAL_SECONDS = 5.0
GZIP_LEVEL = 6  # Fast compression for CSV text

# Lifecycle statuses for an import file
IMPORTED = "IMPORTED"
QUEUED = "QUEUED"
INVALID = "INVALID"
ALREADY_UPLOADED = "ALREADY UPLOADED"
FAILED = "FAILED"
ERROR = "ERROR"
SUMMARY_ORDER = (IMPORTED, QUEUED, INVALID, ALREADY_UPLOADED, FAILED, ERROR)


@dataclass(frozen=True)
class FileOutcome:
    """The result of an upload or worker import attempt for one file."""
    path: Path
    status: str
    detail: str
    batch_id: uuid.UUID | None = None


@dataclass(frozen=True)
class BatchRegistration:
    """The result of registering a batch in the database."""
    batch: ImportBatch
    is_new: bool


def utc_now() -> datetime:
    """Returns current UTC time or the frozen test time if X-Test-Clock is active."""
    return clock.utc_now()


def register_import_batch(
    session: Session, filename: str, file_sha256: str
) -> BatchRegistration:
    """Registers a new batch or returns the existing batch if the SHA-256 hash matches.
    
    Relies on a database UNIQUE constraint on `file_sha256` so concurrent uploads
    of the exact same file cannot create duplicate batches.
    """
    batch = ImportBatch(filename=filename, file_sha256=file_sha256, status="QUEUED")
    try:
        with session.begin_nested():
            session.add(batch)
    except IntegrityError:
        existing = session.scalar(
            select(ImportBatch).where(ImportBatch.file_sha256 == file_sha256)
        )
        if existing is None:
            raise
        return BatchRegistration(existing, is_new=False)
    return BatchRegistration(batch, is_new=True)


def is_claimable():
    """SQL condition identifying batches ready to be processed by a worker.
    
    Includes batches with QUEUED or FAILED status, or PROCESSING batches that
    were abandoned more than 1 hour ago (worker crash).
    """
    return or_(
        ImportBatch.status.in_(("QUEUED", "FAILED")),
        and_(
            ImportBatch.status == "PROCESSING",
            ImportBatch.started_at < utc_now() - STALE_PROCESSING_AFTER,
        ),
    )


def claim_batch(session: Session, batch: ImportBatch) -> bool:
    """Atomically transitions a batch from QUEUED to PROCESSING.
    
    Uses an atomic UPDATE ... WHERE condition. Returns True if this worker
    successfully claimed the batch, or False if another worker claimed it first.
    """
    claimed = session.execute(
        update(ImportBatch)
        .where(ImportBatch.id == batch.id, is_claimable())
        .values(
            status="PROCESSING",
            started_at=utc_now(),
            completed_at=None,
            failure_reason=None,
            attempt_count=ImportBatch.attempt_count + 1,
        )
        .execution_options(synchronize_session=False)
    )
    session.commit()
    session.refresh(batch)
    return claimed.rowcount == 1


def record_batch(result: FileCheckResult) -> FileOutcome:
    """Direct synchronous import handler (used by CLI when not queueing)."""
    if result.sha256 is None:
        raise ValueError("Only a valid file (with a hash) can be recorded as a batch.")
    try:
        with SessionLocal() as session:
            registration = register_import_batch(
                session, filename=result.path.name, file_sha256=result.sha256
            )
            session.commit()
            batch = registration.batch
            if not claim_batch(session, batch):
                detail = (
                    f"this file was already uploaded as '{batch.filename}' "
                    f"(batch {batch.id}, status {batch.status})."
                )
                print(f"ALREADY UPLOADED {result.path}: {detail}", file=sys.stderr)
                return FileOutcome(result.path, ALREADY_UPLOADED, detail)
            errors = process_batch(session, batch, result)
    except SQLAlchemyError:
        logger.exception("Could not import %s", result.path)
        detail = "could not import the file (database unavailable)."
        print(f"ERROR {result.path}: {detail}", file=sys.stderr)
        return FileOutcome(result.path, ERROR, detail)

    if batch.status != "COMPLETED":
        print(f"Batch {batch.id} FAILED: {batch.failure_reason}", file=sys.stderr)
        return FileOutcome(result.path, FAILED, batch.failure_reason or "")

    return report_completed(result.path, batch, errors)


def report_completed(
    path: Path, batch: ImportBatch, errors: list[RowError]
) -> FileOutcome:
    """Echoes and returns summary statistics for a completed batch."""
    echo(
        f"Batch {batch.id} COMPLETED: {batch.rows_total} rows, "
        f"{batch.rows_accepted} accepted, {batch.rows_rejected} rejected."
    )
    for error in errors:
        echo(f"Row {error.row_number} [{error.column_name}]: {error.message}")
    return FileOutcome(
        path,
        IMPORTED,
        f"batch {batch.id}: {batch.rows_accepted} accepted, "
        f"{batch.rows_rejected} rejected.",
    )


def queue_file(path: Path, max_size_bytes: int) -> FileOutcome:
    """Stages a file from disk into the database queue for worker processing."""
    try:
        content = read_file(path, max_size_bytes)
    except CsvFileError as error:
        print(f"INVALID {path}: {error}", file=sys.stderr)
        return FileOutcome(path, INVALID, str(error))
    try:
        with SessionLocal() as session:
            outcome = enqueue_upload(session, path.name, content, max_size_bytes)
    except SQLAlchemyError:
        logger.exception("Could not queue %s", path)
        outcome = FileOutcome(
            path, ERROR, "could not queue the file (database unavailable)."
        )
    except Exception:
        logger.exception("Unexpected error while queueing %s", path)
        outcome = FileOutcome(path, ERROR, "unexpected error (details are in the log)")
    outcome = replace(outcome, path=path)
    stream = sys.stdout if outcome.status == QUEUED else sys.stderr
    print(f"{outcome.status} {path}: {outcome.detail}", file=stream)
    return outcome


def enqueue_upload(
    session: Session, filename: str, content: bytes, max_size_bytes: int
) -> FileOutcome:
    """Stores the compressed CSV payload in `import_payloads` table in PostgreSQL.
    
    Compresses with Gzip to minimize database storage overhead.
    Returns immediately with QUEUED status so the HTTP endpoint can return 202 Accepted.
    """
    path = Path(filename)
    try:
        check_name_and_size(path, len(content), max_size_bytes)
    except CsvFileError as error:
        return FileOutcome(path, INVALID, str(error))

    registration = register_import_batch(
        session, filename=path.name, file_sha256=hashlib.sha256(content).hexdigest()
    )
    batch = registration.batch
    retry = (
        not registration.is_new
        and batch.status == "FAILED"
        and session.get(ImportPayload, batch.id) is None
    )
    if not (registration.is_new or retry):
        return FileOutcome(
            path,
            ALREADY_UPLOADED,
            f"this file was already uploaded as '{batch.filename}' "
            f"(batch {batch.id}, status {batch.status}).",
            batch_id=batch.id,
        )

    session.add(
        ImportPayload(
            batch_id=batch.id,
            csv_gzip=gzip.compress(content, compresslevel=GZIP_LEVEL),
            received_size_bytes=len(content),
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return FileOutcome(
            path,
            ALREADY_UPLOADED,
            f"this file is already queued (batch {batch.id}).",
            batch_id=batch.id,
        )
    action = "queued again for retry" if retry else "queued"
    return FileOutcome(path, QUEUED, f"batch {batch.id} {action}.", batch_id=batch.id)


def run_worker(
    *,
    drain: bool,
    poll_interval_seconds: float = WORKER_POLL_INTERVAL_SECONDS,
    batch_ids: Collection[uuid.UUID] | None = None,
) -> list[FileOutcome]:
    """Continuous background worker loop that polls for queued files.
    
    drain=True: Processes all pending files and stops (useful for tests and scripts).
    drain=False: Keeps running indefinitely, waking up every 5 seconds.
    """
    outcomes = []
    while True:
        try:
            outcome = run_next_batch(batch_ids)
        except SQLAlchemyError:
            logger.exception("Worker could not reach the database")
            echo("ERROR worker: database unavailable.", to_stderr=True)
            if drain:
                return [
                    *outcomes,
                    FileOutcome(Path("-"), ERROR, "database unavailable."),
                ]
            outcome = None
        if outcome is not None:
            outcomes.append(outcome)
        elif drain:
            return outcomes
        else:
            time.sleep(poll_interval_seconds)


def run_next_batch(
    batch_ids: Collection[uuid.UUID] | None = None,
) -> FileOutcome | None:
    """Claims the next available batch from the queue and imports it."""
    with SessionLocal() as session:
        batch = claim_next_batch(session, batch_ids)
        if batch is None:
            return None
        return import_staged_batch(session, batch)


def claim_next_batch(
    session: Session, batch_ids: Collection[uuid.UUID] | None = None
) -> ImportBatch | None:
    """Finds the oldest unprocessed batch and attempts to claim it."""
    only_these = [ImportBatch.id.in_(batch_ids)] if batch_ids is not None else []
    candidates = session.scalars(
        select(ImportBatch)
        .join(ImportPayload, ImportPayload.batch_id == ImportBatch.id)
        .where(is_claimable(), *only_these)
        .order_by(ImportBatch.uploaded_at, ImportBatch.id)
        .limit(10)
    ).all()
    session.commit()
    for batch in candidates:
        if claim_batch(session, batch):
            return batch
    return None


def import_staged_batch(session: Session, batch: ImportBatch) -> FileOutcome:
    """Decompresses the staged file payload and processes it into the database.
    
    Deletes the gzipped payload from `import_payloads` once processed to free up storage.
    """
    path = Path(batch.filename)
    payload = session.get(ImportPayload, batch.id)
    if payload is None:
        return finish_without_import(
            session, batch, None, "FAILED", "Staged file is missing."
        )
    try:
        content = gzip.decompress(payload.csv_gzip)
    except (OSError, EOFError):
        return finish_without_import(
            session, batch, payload, "FAILED", "Staged file is corrupt."
        )

    result = check_csv_content(path, content)
    if not result.is_valid:
        status = "REJECTED_HEADER" if result.is_header_problem else "FAILED"
        return finish_without_import(session, batch, payload, status, result.message)

    session.delete(payload)
    errors = process_batch(session, batch, result)
    if batch.status == "COMPLETED":
        return report_completed(path, batch, errors)
    return schedule_retry_or_give_up(session, batch)


def schedule_retry_or_give_up(session: Session, batch: ImportBatch) -> FileOutcome:
    """Handles failure retries up to MAX_IMPORT_ATTEMPTS (3 times)."""
    attempts = batch.attempt_count
    if attempts < MAX_IMPORT_ATTEMPTS:
        batch.failure_reason = (
            f"Database error on attempt {attempts} of {MAX_IMPORT_ATTEMPTS}; "
            "nothing was saved. The worker will retry."
        )
    else:
        payload = session.get(ImportPayload, batch.id)
        if payload is not None:
            session.delete(payload)
        batch.failure_reason = (
            f"Database error on all {MAX_IMPORT_ATTEMPTS} attempts; nothing was "
            "saved. Upload the same file again to retry."
        )
    session.commit()
    logger.warning(
        "Batch %s failed: %s",
        batch.id,
        batch.failure_reason,
        extra={
            "batch_id": batch.id,
            "file_name": batch.filename,
            "attempt": attempts,
            "max_attempts": MAX_IMPORT_ATTEMPTS,
            "will_retry": attempts < MAX_IMPORT_ATTEMPTS,
        },
    )
    echo(f"Batch {batch.id} FAILED: {batch.failure_reason}", to_stderr=True)
    return FileOutcome(Path(batch.filename), FAILED, batch.failure_reason)


def finish_without_import(
    session: Session,
    batch: ImportBatch,
    payload: ImportPayload | None,
    status: str,
    reason: str,
) -> FileOutcome:
    """Permanently closes a batch whose file could not even be parsed."""
    if payload is not None:
        session.delete(payload)
    batch.status = status
    batch.failure_reason = reason
    batch.completed_at = utc_now()
    session.commit()
    logger.warning(
        "Batch %s %s: %s",
        batch.id,
        status,
        reason,
        extra={"batch_id": batch.id, "file_name": batch.filename, "status": status},
    )
    echo(f"Batch {batch.id} {status}: {reason}", to_stderr=True)
    return FileOutcome(Path(batch.filename), INVALID, reason)


def import_file(path: Path, max_size_bytes: int) -> FileOutcome:
    """Helper used to parse and synchronously import a single file from disk."""
    try:
        result = check_csv_file(path, max_size_bytes)
        if not result.is_valid:
            print(f"INVALID {result.path}: {result.message}", file=sys.stderr)
            return FileOutcome(path, INVALID, result.message)
        print(f"{result.sha256}  {result.path}")
        return record_batch(result)
    except Exception:
        logger.exception("Unexpected error while importing %s", path)
        print(
            f"ERROR {path}: unexpected error (details are in the log).", file=sys.stderr
        )
        return FileOutcome(path, ERROR, "unexpected error (details are in the log)")


def print_summary(outcomes: list[FileOutcome]) -> None:
    """Prints a neat terminal summary table when processing multiple files."""
    counts = Counter(outcome.status for outcome in outcomes)
    totals = ", ".join(
        f"{counts[status]} {status.lower()}"
        for status in SUMMARY_ORDER
        if counts[status]
    )
    width = max(len(status) for status in counts)
    print()
    print(f"Summary: {len(outcomes)} files: {totals}.")
    for outcome in outcomes:
        print(f"  {outcome.status:<{width}}  {outcome.path}: {outcome.detail}")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point supporting `--queue`, `--worker`, and `--once` flags."""
    parser = argparse.ArgumentParser(
        description="Validate sales CSV files and import their rows."
    )
    parser.add_argument(
        "paths", nargs="*", type=Path, metavar="path", help="CSV file(s) to import"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--queue",
        action="store_true",
        help="stage the files for the worker and return immediately",
    )
    mode.add_argument(
        "--worker", action="store_true", help="import staged files as they arrive"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="with --worker: import everything staged, then exit",
    )
    args = parser.parse_args(argv)
    if args.worker and args.paths:
        parser.error("--worker takes no file paths")
    if not args.worker and not args.paths:
        parser.error("at least one file path is required")
    if args.once and not args.worker:
        parser.error("--once only works with --worker")

    if args.worker:
        try:
            outcomes = run_worker(drain=args.once)
        except KeyboardInterrupt:
            print("Worker stopped.")
            return 0
        expected = IMPORTED
    else:
        max_size_bytes = get_settings().max_upload_size_bytes
        handle = queue_file if args.queue else import_file
        outcomes = [handle(path, max_size_bytes) for path in args.paths]
        expected = QUEUED if args.queue else IMPORTED
    if len(outcomes) > 1:
        print_summary(outcomes)
    return 0 if all(outcome.status == expected for outcome in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())