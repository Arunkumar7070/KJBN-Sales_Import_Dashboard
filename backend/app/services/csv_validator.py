"""File-level checks for uploaded sales CSV files.

Checks existence, extension, size, readability, UTF-8 encoding (an optional
BOM is allowed), CSV syntax and the header row. LF and CRLF line endings are
both accepted. Columns may appear in any order; rows are mapped by header name.
Only a file that passes every check gets the SHA-256 hash of its original bytes.
Each data row must then have exactly one value per column and every required
value; invalid rows are recorded and never fail the file.

The command line records an import batch for a valid file (or reports that the
same file was already uploaded), then imports its rows: duplicates in the file
or database and rows breaking the sales rules (app.services.sales_rules) go to
import_row_errors, valid rows go to transactions, and the batch gets its counts
and final status.

Usage (from backend/):
    python -m app.services.csv_validator sales.csv [more.csv ...]   # import now
    python -m app.services.csv_validator --queue sales.csv [...]     # stage only
    python -m app.services.csv_validator --worker [--once]           # import staged

--queue stores each file gzipped in import_payloads with a QUEUED batch and
returns at once. --worker imports staged batches oldest first and deletes each
payload in the same commit as the imported rows (--once: stop when idle).

Each file is handled on its own: an invalid, duplicate or failing file is
reported and the remaining files are still imported. With two or more files a
summary is printed at the end. The exit code is 0 only if every file imported.
"""

import argparse
import csv
import gzip
import hashlib
import io
import logging
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    ImportBatch,
    ImportPayload,
    ImportRowError,
    Product,
    ProductAlias,
    Store,
    Transaction,
)
from app.services.sales_rules import (
    SaleRow,
    SaleRuleViolation,
    line_total_paise,
    parse_quantity,
    parse_sale_row,
    parse_sold_at,
    validate_sale,
)

logger = logging.getLogger(__name__)

LOOKUP_CHUNK_SIZE = 1000
# Worker settings: a database error is retried this many times in total, and a
# batch left PROCESSING this long (its worker died) may be claimed again.
MAX_IMPORT_ATTEMPTS = 3
STALE_PROCESSING_AFTER = timedelta(hours=1)
WORKER_POLL_INTERVAL_SECONDS = 5.0
GZIP_LEVEL = 6  # Good compression for CSV text at a fraction of level 9's cost.

REQUIRED_HEADERS = (
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
)


OPTIONAL_FIELDS = frozenset({"original_transaction_id"})  # only set on returns
# Stored as column_name for problems with the row as a whole, not one field.
WHOLE_ROW = "__row__"


@dataclass(frozen=True)
class CsvRow:
    # CSV row number as a spreadsheet shows it: the header is row 1.
    row_number: int
    values: dict[str, str]


@dataclass(frozen=True)
class RowError:
    row_number: int
    column_name: str
    message: str
    transaction_id: str | None = None


@dataclass(frozen=True)
class FileCheckResult:
    path: Path
    is_valid: bool
    message: str
    # Rows that passed row checks; empty unless the file is valid.
    rows: tuple[CsvRow, ...] = ()
    # Rows that failed row checks; they do not make the file invalid.
    row_errors: tuple[RowError, ...] = ()
    # SHA-256 of the original file bytes (BOM included); None unless valid.
    sha256: str | None = None
    # True when the header row was the problem (batch status REJECTED_HEADER).
    is_header_problem: bool = False


class CsvFileError(Exception):
    """A problem that makes the whole file unusable."""


class CsvHeaderError(CsvFileError):
    """The header row is missing or wrong."""


def check_csv_file(path: Path, max_size_bytes: int) -> FileCheckResult:
    try:
        content = read_file(path, max_size_bytes)
    except CsvFileError as error:
        return FileCheckResult(path, is_valid=False, message=str(error))
    return check_csv_content(path, content)


def check_csv_content(path: Path, content: bytes) -> FileCheckResult:
    """Encoding, CSV syntax, header and row checks on bytes already in memory."""
    try:
        text = decode_utf8(content)
        header, *records = parse_records(text)
        header_problem = find_header_problem(header)
        if header_problem:
            raise CsvHeaderError(header_problem)
    except CsvFileError as error:
        return FileCheckResult(
            path,
            is_valid=False,
            message=str(error),
            is_header_problem=isinstance(error, CsvHeaderError),
        )

    rows, row_errors = validate_rows(header, records)
    return FileCheckResult(
        path,
        is_valid=True,
        message="File passed all checks.",
        rows=rows,
        row_errors=row_errors,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def read_file(path: Path, max_size_bytes: int) -> bytes:
    if not path.exists():
        raise CsvFileError("File does not exist.")
    if not path.is_file():
        raise CsvFileError("Path is not a regular file.")

    try:
        # Size is checked before reading so oversized files are never loaded.
        check_name_and_size(path, path.stat().st_size, max_size_bytes)
        return path.read_bytes()
    except OSError as error:
        raise CsvFileError(
            f"File cannot be read ({error.strerror or 'unknown error'})."
        ) from error


def check_name_and_size(path: Path, size_bytes: int, max_size_bytes: int) -> None:
    if path.suffix.lower() != ".csv":
        raise CsvFileError("File must have a .csv extension.")
    if size_bytes == 0:
        raise CsvFileError("File is empty.")
    if size_bytes > max_size_bytes:
        raise CsvFileError(
            f"File is {size_bytes} bytes, which exceeds the {max_size_bytes}-byte limit."
        )


def decode_utf8(content: bytes) -> str:
    try:
        # Decoding as plain UTF-8 (not utf-8-sig) keeps error.start a true file
        # offset even when a BOM is present; the BOM is stripped afterwards.
        text = content.decode("utf-8").removeprefix("﻿")
    except UnicodeDecodeError as error:
        raise CsvFileError(
            f"File is not valid UTF-8 (invalid byte at position {error.start})."
        ) from error
    if not text:
        raise CsvFileError("File is empty.")
    return text


def parse_records(text: str) -> list[list[str]]:
    # newline="" lets the csv module handle LF, CRLF and newlines inside quotes.
    # strict=True rejects unclosed quotes and text after a closing quote, which
    # the default mode silently accepts. A plain reader (not DictReader) keeps
    # each record's real field count for the row checks.
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        return list(reader)
    except csv.Error as error:
        raise CsvFileError(
            f"Malformed CSV near line {reader.line_num}: {error}."
        ) from error


def validate_rows(
    header: list[str], records: list[list[str]]
) -> tuple[tuple[CsvRow, ...], tuple[RowError, ...]]:
    rows, errors = [], []
    # Records are numbered like spreadsheet rows: header is 1, so data starts at 2.
    for row_number, record in enumerate(records, start=2):
        if not record:
            continue  # Blank line: nothing to import, but it still takes a number.
        outcome = validate_row(header, record, row_number)
        (errors if isinstance(outcome, RowError) else rows).append(outcome)
    return tuple(rows), tuple(errors)


def validate_row(
    header: list[str], record: list[str], row_number: int
) -> CsvRow | RowError:
    if len(record) != len(header):
        return RowError(
            row_number,
            WHOLE_ROW,
            f"Row has {len(record)} column(s); expected {len(header)}.",
        )

    values = dict(zip(header, record, strict=True))
    missing = [
        name
        for name in REQUIRED_HEADERS
        if name not in OPTIONAL_FIELDS and not values[name].strip()
    ]
    if missing:
        # One error per row (import_row_errors is unique on batch_id, row_number);
        # column_name points at the first missing field, the message lists all.
        return RowError(
            row_number,
            missing[0],
            f"Missing required value(s): {', '.join(missing)}.",
            transaction_id=values["transaction_id"].strip() or None,
        )
    return CsvRow(row_number, values)


def find_header_problem(header: Sequence[str] | None) -> str | None:
    if not header:
        return "Header row is missing."

    names = [name for name in header if name.strip()]
    blank_positions = [
        str(position) for position, name in enumerate(header, 1) if not name.strip()
    ]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    missing = [name for name in REQUIRED_HEADERS if name not in names]
    unexpected = [
        describe_unexpected(name)
        for name in dict.fromkeys(names)
        if name not in REQUIRED_HEADERS
    ]

    problems = []
    if missing:
        problems.append(f"missing column(s): {', '.join(missing)}")
    if unexpected:
        problems.append(f"unexpected column(s): {', '.join(unexpected)}")
    if duplicates:
        problems.append(f"duplicate column(s): {', '.join(duplicates)}")
    if blank_positions:
        problems.append(
            f"blank column name(s) at position(s): {', '.join(blank_positions)}"
        )
    return f"Invalid header: {'; '.join(problems)}." if problems else None


def describe_unexpected(name: str) -> str:
    # Point out near-misses such as "SKU" or " sku" so the fix is obvious.
    normalized = name.strip().lower()
    if normalized in REQUIRED_HEADERS:
        return f"'{name}' (expected '{normalized}')"
    return f"'{name}'"


IMPORTED = "IMPORTED"
QUEUED = "QUEUED"
INVALID = "INVALID"
ALREADY_UPLOADED = "ALREADY UPLOADED"
FAILED = "FAILED"
ERROR = "ERROR"
SUMMARY_ORDER = (IMPORTED, QUEUED, INVALID, ALREADY_UPLOADED, FAILED, ERROR)


@dataclass(frozen=True)
class FileOutcome:
    path: Path
    status: str  # One of SUMMARY_ORDER.
    detail: str
    batch_id: uuid.UUID | None = None


def main(argv: list[str] | None = None) -> int:
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


def import_file(path: Path, max_size_bytes: int) -> FileOutcome:
    """Validate and import one file; never raises, so one bad file can't stop the rest."""
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


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class BatchRegistration:
    batch: ImportBatch
    is_new: bool


def register_import_batch(
    session: Session, filename: str, file_sha256: str
) -> BatchRegistration:
    """Create a QUEUED batch, or return the existing batch for the same file hash.

    Duplicates are detected by the unique constraint on file_sha256, not by a
    lookup first, so two simultaneous uploads of the same file cannot both
    create a batch. The caller owns the transaction and must commit.
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
            raise  # Some other constraint failed; not a duplicate upload.
        return BatchRegistration(existing, is_new=False)
    return BatchRegistration(batch, is_new=True)


def is_claimable():
    """QUEUED or FAILED batches, or PROCESSING ones whose worker has died."""
    return or_(
        ImportBatch.status.in_(("QUEUED", "FAILED")),
        and_(
            ImportBatch.status == "PROCESSING",
            ImportBatch.started_at < utc_now() - STALE_PROCESSING_AFTER,
        ),
    )


def claim_batch(session: Session, batch: ImportBatch) -> bool:
    """Move a claimable batch to PROCESSING; False if someone else has it.

    The status condition in the UPDATE makes the claim atomic, so two
    processes can never import the same batch at once.
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
        # Skip re-evaluating the WHERE in Python on loaded objects; the batch is
        # refreshed from the database below instead.
        .execution_options(synchronize_session=False)
    )
    session.commit()
    session.refresh(batch)
    return claimed.rowcount == 1


def process_batch(
    session: Session, batch: ImportBatch, result: FileCheckResult
) -> list[RowError]:
    """Import all rows in one transaction and record the outcome on the batch.

    Rejected rows never stop the import. A database failure rolls back every
    row, so a batch is never half-imported; it is marked FAILED and can be
    retried by uploading the same file again.
    """
    try:
        accepted, errors = import_rows(session, batch, result, clock=utc_now)
        batch.rows_total = accepted + len(errors)
        batch.rows_accepted = accepted
        batch.rows_rejected = len(errors)
        batch.status = "COMPLETED"
        batch.completed_at = utc_now()
        session.commit()
        return errors
    except SQLAlchemyError:
        session.rollback()
        logger.exception("Import of batch %s failed", batch.id)
        batch.status = "FAILED"
        batch.failure_reason = (
            "Database error while saving rows; nothing was saved. "
            "Upload the same file again to retry."
        )
        batch.completed_at = utc_now()
        session.commit()
        return []


@dataclass
class ImportState:
    """Database lookups for this file plus everything accepted so far."""

    existing_transaction_ids: set[str]
    store_codes: set[str]
    product_names: dict[str, str]  # sku -> name
    aliases: set[tuple[str, str]]  # (sku, alias_name)
    originals: dict[str, SaleRow]  # transaction_id -> row (database or this file)
    returned_quantity: Counter[str]  # original transaction_id -> units returned
    accepted_row_numbers: dict[str, int] = field(default_factory=dict)


def import_rows(
    session: Session,
    batch: ImportBatch,
    result: FileCheckResult,
    clock: Callable[[], datetime],
) -> tuple[int, list[RowError]]:
    state = load_import_state(session, result.rows)
    errors = list(result.row_errors)
    accepted = 0
    for row in processing_order(result.rows):
        try:
            sale = accept_row(row.values, state, clock)
            save_row(session, state, batch, sale, row)
        except SaleRuleViolation as violation:
            errors.append(
                RowError(
                    row.row_number,
                    violation.column_name,
                    violation.message,
                    transaction_id=row.values["transaction_id"],
                )
            )
            continue
        accepted += 1

    errors.sort(key=lambda error: error.row_number)
    session.add_all(
        ImportRowError(
            batch_id=batch.id,
            row_number=error.row_number,
            column_name=error.column_name,
            message=error.message,
            transaction_id=error.transaction_id,
        )
        for error in errors
    )
    return accepted, errors


def processing_order(rows: Sequence[CsvRow]) -> list[CsvRow]:
    """Order rows in two passes: every sale first, then every return.

    Pass 1 holds all rows that are not returns (including rows that will be
    rejected anyway), so by Pass 2 every sale in the file has been accepted or
    rejected and any return can find its original wherever it was listed.
    Each pass runs oldest sold_at first, so cumulative return totals and
    duplicate IDs are resolved in time order. Rows are only reordered, never
    changed: row_number stays the original CSV row for error reports.
    """
    sales_pass, returns_pass = [], []
    for row in rows:
        (returns_pass if is_return_row(row) else sales_pass).append(row)
    return sorted(sales_pass, key=chronological_key) + sorted(
        returns_pass, key=chronological_key
    )


def is_return_row(row: CsvRow) -> bool:
    try:
        return parse_quantity(row.values["quantity"]) < 0
    except SaleRuleViolation:
        return False  # Rejected later with a clear quantity error.


def chronological_key(row: CsvRow) -> tuple[bool, datetime, int]:
    # Rows with an unreadable sold_at are rejected anyway; they go last in
    # their pass, in file order. row_number breaks ties.
    try:
        return (False, parse_sold_at(row.values["sold_at"]), row.row_number)
    except SaleRuleViolation:
        return (True, datetime.max.replace(tzinfo=UTC), row.row_number)


def accept_row(
    values: dict[str, str], state: ImportState, clock: Callable[[], datetime]
) -> SaleRow:
    transaction_id = values["transaction_id"]
    if transaction_id in state.accepted_row_numbers:
        first_row = state.accepted_row_numbers[transaction_id]
        raise SaleRuleViolation(
            "DUPLICATE_IN_FILE",
            f"Transaction ID '{transaction_id}' already appears in row {first_row}.",
        )
    if transaction_id in state.existing_transaction_ids:
        raise SaleRuleViolation(
            "DUPLICATE_IN_DATABASE",
            f"Transaction ID '{transaction_id}' has already been imported.",
        )

    sale = parse_sale_row(values)
    if sale.store_code not in state.store_codes:
        raise SaleRuleViolation(
            "UNKNOWN_STORE", f"Store code '{sale.store_code}' does not exist."
        )
    original_id = sale.original_transaction_id
    validate_sale(
        sale,
        clock=clock,
        original=state.originals.get(original_id) if original_id else None,
        already_returned_quantity=state.returned_quantity[original_id]
        if original_id
        else 0,
    )
    return sale


def save_row(
    session: Session,
    state: ImportState,
    batch: ImportBatch,
    sale: SaleRow,
    row: CsvRow,
) -> None:
    """Insert one accepted row (and any new product or alias) in its own savepoint.

    Another import can commit the same transaction ID, SKU or alias after this
    import's lookups ran. PostgreSQL then rejects only this row's INSERT, and
    rolling back just its savepoint keeps every other row of the batch:
    - transaction ID now exists: the row is a duplicate (DUPLICATE_IN_DATABASE);
    - otherwise a new product or alias was created by the other import:
      reload it and insert the row again, once.
    State is updated only after the row's savepoint succeeds.
    """
    for attempt in (1, 2):
        try:
            with session.begin_nested():  # Flushes on exit, so conflicts surface here.
                new_product, new_alias = add_row(session, state, batch, sale, row)
        except IntegrityError:
            if transaction_exists(session, sale.transaction_id):
                raise SaleRuleViolation(
                    "DUPLICATE_IN_DATABASE",
                    f"Transaction ID '{sale.transaction_id}' has already been "
                    "imported.",
                ) from None
            if attempt == 2:
                raise  # Not a known race; let the batch fail.
            reload_product(session, state, sale.sku)
            continue
        break

    if new_product:
        state.product_names[sale.sku] = new_product.name
    if new_alias:
        state.aliases.add((sale.sku, new_alias.alias_name))
    state.accepted_row_numbers[sale.transaction_id] = row.row_number
    state.originals[sale.transaction_id] = sale
    if sale.original_transaction_id:
        state.returned_quantity[sale.original_transaction_id] += -sale.quantity


def add_row(
    session: Session,
    state: ImportState,
    batch: ImportBatch,
    sale: SaleRow,
    row: CsvRow,
) -> tuple[Product | None, ProductAlias | None]:
    product_name = row.values["product_name"]
    known_name = state.product_names.get(sale.sku)
    new_product = new_alias = None
    if known_name is None:
        new_product = Product(sku=sale.sku, name=product_name)
        session.add(new_product)
    elif product_name != known_name and (sale.sku, product_name) not in state.aliases:
        new_alias = ProductAlias(sku=sale.sku, alias_name=product_name)
        session.add(new_alias)

    session.add(
        Transaction(
            transaction_id=sale.transaction_id,
            store_code=sale.store_code,
            sold_at=sale.sold_at,
            sku=sale.sku,
            product_name=product_name,
            quantity=sale.quantity,
            unit_price_paise=sale.unit_price_paise,
            discount_paise=sale.discount_paise,
            payment_mode=sale.payment_mode,
            original_transaction_id=sale.original_transaction_id,
            batch_id=batch.id,
            line_total_paise=line_total_paise(sale),
            is_return=sale.quantity < 0,
        )
    )
    return new_product, new_alias


def transaction_exists(session: Session, transaction_id: str) -> bool:
    return (
        session.scalar(
            select(Transaction.transaction_id).where(
                Transaction.transaction_id == transaction_id
            )
        )
        is not None
    )


def reload_product(session: Session, state: ImportState, sku: str) -> None:
    name = session.scalar(select(Product.name).where(Product.sku == sku))
    if name is not None:
        state.product_names[sku] = name
    state.aliases.update(
        (sku, alias)
        for alias in session.scalars(
            select(ProductAlias.alias_name).where(ProductAlias.sku == sku)
        )
    )


def load_import_state(session: Session, rows: Sequence[CsvRow]) -> ImportState:
    values = [row.values for row in rows]
    transaction_ids = {v["transaction_id"] for v in values}
    original_ids = {
        v["original_transaction_id"] for v in values if v["original_transaction_id"]
    }
    skus = {v["sku"] for v in values}
    store_codes = {v["store_code"] for v in values}

    # Column order matches SaleRow's fields, so each row unpacks into one.
    original_columns = select(
        Transaction.transaction_id,
        Transaction.store_code,
        Transaction.sku,
        Transaction.sold_at,
        Transaction.quantity,
        Transaction.unit_price_paise,
        Transaction.discount_paise,
        Transaction.payment_mode,
        Transaction.original_transaction_id,
    )
    returned_by_original = (
        select(Transaction.original_transaction_id, func.sum(-Transaction.quantity))
        .where(Transaction.quantity < 0)
        .group_by(Transaction.original_transaction_id)
    )

    # Lock the original sales (SELECT ... FOR UPDATE) before reading how much
    # of them was already returned. A concurrent import returning against the
    # same sale blocks here until this import commits, then reads the returns
    # it saved, so two imports can never both use the same remaining quantity.
    # Locks are taken in transaction_id order (ORDER BY, and sorted chunks), so
    # every import locks in the same order and two imports cannot deadlock.
    # The locks are held until process_batch commits or rolls back.
    originals = {
        row[0]: sale_row_from_database(row)
        for row in fetch_where_in(
            session,
            original_columns.order_by(Transaction.transaction_id).with_for_update(),
            Transaction.transaction_id,
            original_ids,
        )
    }
    # Read only after the locks are held, so it includes every committed return.
    returned_quantity = Counter(
        dict(
            fetch_where_in(
                session,
                returned_by_original,
                Transaction.original_transaction_id,
                original_ids,
            )
        )
    )

    return ImportState(
        existing_transaction_ids={
            transaction_id
            for (transaction_id,) in fetch_where_in(
                session,
                select(Transaction.transaction_id),
                Transaction.transaction_id,
                transaction_ids,
            )
        },
        store_codes={
            code
            for (code,) in fetch_where_in(
                session, select(Store.code), Store.code, store_codes
            )
        },
        product_names=dict(
            fetch_where_in(
                session, select(Product.sku, Product.name), Product.sku, skus
            )
        ),
        aliases={
            (sku, alias)
            for sku, alias in fetch_where_in(
                session,
                select(ProductAlias.sku, ProductAlias.alias_name),
                ProductAlias.sku,
                skus,
            )
        },
        originals=originals,
        returned_quantity=returned_quantity,
    )


def sale_row_from_database(row) -> SaleRow:
    sale = SaleRow(*row)
    # sold_at is stored in UTC, but some drivers (e.g. SQLite) drop the
    # timezone on read; restore it so it compares with parsed CSV times.
    if sale.sold_at.tzinfo is None:
        return replace(sale, sold_at=sale.sold_at.replace(tzinfo=UTC))
    return sale


def fetch_where_in(session: Session, statement, column, values: set[str]) -> list:
    # Chunked so a large file never exceeds the database's bind-parameter limit.
    ordered = sorted(values)
    results = []
    for start in range(0, len(ordered), LOOKUP_CHUNK_SIZE):
        chunk = ordered[start : start + LOOKUP_CHUNK_SIZE]
        results.extend(session.execute(statement.where(column.in_(chunk))).all())
    return results


def record_batch(result: FileCheckResult) -> FileOutcome:
    if result.sha256 is None:
        raise ValueError("Only a valid file (with a hash) can be recorded as a batch.")
    try:
        with SessionLocal() as session:
            registration = register_import_batch(
                session, filename=result.path.name, file_sha256=result.sha256
            )
            session.commit()
            batch = registration.batch
            # The claim only succeeds for QUEUED or FAILED batches, so uploading
            # a file again retries one that never ran or failed (nothing was
            # saved), and is refused if it is processing or already completed.
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
    print(
        f"Batch {batch.id} COMPLETED: {batch.rows_total} rows, "
        f"{batch.rows_accepted} accepted, {batch.rows_rejected} rejected."
    )
    for error in errors:
        print(f"Row {error.row_number} [{error.column_name}]: {error.message}")
    return FileOutcome(
        path,
        IMPORTED,
        f"batch {batch.id}: {batch.rows_accepted} accepted, "
        f"{batch.rows_rejected} rejected.",
    )


# Staged uploads and the background worker


def queue_file(path: Path, max_size_bytes: int) -> FileOutcome:
    """Stage one file from disk for the worker; never raises (see import_file)."""
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
    """Stage an upload: a QUEUED batch plus its gzipped bytes, in one commit.

    Only cheap checks run here (extension, empty, size), so the caller returns
    at once; the worker does the parsing and importing. A file that already
    failed permanently (its payload was removed) is staged again for a retry.
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
    except IntegrityError:  # Another upload of the same file staged it first.
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
    *, drain: bool, poll_interval_seconds: float = WORKER_POLL_INTERVAL_SECONDS
) -> list[FileOutcome]:
    """Import staged batches one at a time, oldest first.

    With drain=True, stop when nothing is left (or the database is down);
    otherwise keep polling until interrupted (Ctrl+C).
    """
    outcomes = []
    while True:
        try:
            outcome = run_next_batch()
        except SQLAlchemyError:
            logger.exception("Worker could not reach the database")
            print("ERROR worker: database unavailable.", file=sys.stderr)
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


def run_next_batch() -> FileOutcome | None:
    """Claim the oldest batch with a staged payload and import it; None if idle."""
    with SessionLocal() as session:
        batch = claim_next_batch(session)
        if batch is None:
            return None
        return import_staged_batch(session, batch)


def claim_next_batch(session: Session) -> ImportBatch | None:
    # Only batches with a payload have work left. Several candidates are tried
    # because another worker may claim the first one between SELECT and UPDATE.
    candidates = session.scalars(
        select(ImportBatch)
        .join(ImportPayload, ImportPayload.batch_id == ImportBatch.id)
        .where(is_claimable())
        .order_by(ImportBatch.uploaded_at, ImportBatch.id)
        .limit(10)
    ).all()
    session.commit()  # End the read so the claims below start fresh.
    for batch in candidates:
        if claim_batch(session, batch):
            return batch
    return None


def import_staged_batch(session: Session, batch: ImportBatch) -> FileOutcome:
    path = Path(batch.filename)
    payload = session.get(ImportPayload, batch.id)
    if payload is None:  # Removed after this batch was claimed; nothing to do.
        return finish_without_import(
            session, batch, None, "FAILED", "Staged file is missing."
        )
    try:
        content = gzip.decompress(payload.csv_gzip)
    except (OSError, EOFError):  # gzip.BadGzipFile is an OSError.
        return finish_without_import(
            session, batch, payload, "FAILED", "Staged file is corrupt."
        )

    result = check_csv_content(path, content)
    if not result.is_valid:
        status = "REJECTED_HEADER" if result.is_header_problem else "FAILED"
        return finish_without_import(session, batch, payload, status, result.message)

    # Deleted in the same transaction that saves the rows: on success both are
    # committed together; on a database error the rollback keeps the payload.
    session.delete(payload)
    errors = process_batch(session, batch, result)
    if batch.status == "COMPLETED":
        return report_completed(path, batch, errors)
    return schedule_retry_or_give_up(session, batch)


def schedule_retry_or_give_up(session: Session, batch: ImportBatch) -> FileOutcome:
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
    print(f"Batch {batch.id} FAILED: {batch.failure_reason}", file=sys.stderr)
    return FileOutcome(Path(batch.filename), FAILED, batch.failure_reason)


def finish_without_import(
    session: Session,
    batch: ImportBatch,
    payload: ImportPayload | None,
    status: str,
    reason: str,
) -> FileOutcome:
    """End a batch whose file can never import: retrying the same bytes won't help."""
    if payload is not None:
        session.delete(payload)
    batch.status = status
    batch.failure_reason = reason
    batch.completed_at = utc_now()
    session.commit()
    print(f"Batch {batch.id} {status}: {reason}", file=sys.stderr)
    return FileOutcome(Path(batch.filename), INVALID, reason)


if __name__ == "__main__":
    sys.exit(main())
