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
    python -m app.services.csv_validator sales.csv
"""

import argparse
import csv
import hashlib
import io
import logging
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    ImportBatch,
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
    parse_sale_row,
    validate_sale,
)

logger = logging.getLogger(__name__)

LOOKUP_CHUNK_SIZE = 1000

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


class CsvFileError(Exception):
    """A problem that makes the whole file unusable."""


def check_csv_file(path: Path, max_size_bytes: int) -> FileCheckResult:
    try:
        content = read_file(path, max_size_bytes)
        text = decode_utf8(content)
        header, *records = parse_records(text)
        header_problem = find_header_problem(header)
        if header_problem:
            raise CsvFileError(header_problem)
    except CsvFileError as error:
        return FileCheckResult(path, is_valid=False, message=str(error))

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
    if path.suffix.lower() != ".csv":
        raise CsvFileError("File must have a .csv extension.")

    try:
        size_bytes = path.stat().st_size
        # Size is checked before reading so oversized files are never loaded.
        if size_bytes == 0:
            raise CsvFileError("File is empty.")
        if size_bytes > max_size_bytes:
            raise CsvFileError(
                f"File is {size_bytes} bytes, which exceeds the {max_size_bytes}-byte limit."
            )
        return path.read_bytes()
    except OSError as error:
        raise CsvFileError(
            f"File cannot be read ({error.strerror or 'unknown error'})."
        ) from error


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate a sales CSV file and import its rows."
    )
    parser.add_argument("path", type=Path, help="CSV file path")
    args = parser.parse_args(argv)

    result = check_csv_file(args.path, get_settings().max_upload_size_bytes)
    if not result.is_valid:
        print(f"INVALID {result.path}: {result.message}", file=sys.stderr)
        return 1

    print(f"{result.sha256}  {result.path}")
    return record_batch(result)


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


def claim_batch(session: Session, batch: ImportBatch) -> bool:
    """Move a QUEUED or FAILED batch to PROCESSING; False if someone else has it.

    The status condition in the UPDATE makes the claim atomic, so two
    processes can never import the same batch at once.
    """
    claimed = session.execute(
        update(ImportBatch)
        .where(
            ImportBatch.id == batch.id,
            ImportBatch.status.in_(("QUEUED", "FAILED")),
        )
        .values(
            status="PROCESSING",
            started_at=utc_now(),
            completed_at=None,
            failure_reason=None,
            attempt_count=ImportBatch.attempt_count + 1,
        )
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
    for row in result.rows:
        try:
            sale = accept_row(row.values, state, clock)
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
        save_transaction(session, state, batch, sale, row)
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


def save_transaction(
    session: Session,
    state: ImportState,
    batch: ImportBatch,
    sale: SaleRow,
    row: CsvRow,
) -> None:
    product_name = row.values["product_name"]
    known_name = state.product_names.get(sale.sku)
    if known_name is None:
        session.add(Product(sku=sale.sku, name=product_name))
        state.product_names[sale.sku] = product_name
    elif product_name != known_name and (sale.sku, product_name) not in state.aliases:
        session.add(ProductAlias(sku=sale.sku, alias_name=product_name))
        state.aliases.add((sale.sku, product_name))

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
    state.accepted_row_numbers[sale.transaction_id] = row.row_number
    state.originals[sale.transaction_id] = sale
    if sale.original_transaction_id:
        state.returned_quantity[sale.original_transaction_id] += -sale.quantity


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
        originals={
            row[0]: SaleRow(*row)
            for row in fetch_where_in(
                session, original_columns, Transaction.transaction_id, original_ids
            )
        },
        returned_quantity=Counter(
            dict(
                fetch_where_in(
                    session,
                    returned_by_original,
                    Transaction.original_transaction_id,
                    original_ids,
                )
            )
        ),
    )


def fetch_where_in(session: Session, statement, column, values: set[str]) -> list:
    # Chunked so a large file never exceeds the database's bind-parameter limit.
    ordered = sorted(values)
    results = []
    for start in range(0, len(ordered), LOOKUP_CHUNK_SIZE):
        chunk = ordered[start : start + LOOKUP_CHUNK_SIZE]
        results.extend(session.execute(statement.where(column.in_(chunk))).all())
    return results


def record_batch(result: FileCheckResult) -> int:
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
                print(
                    f"ALREADY UPLOADED {result.path}: this file was already uploaded "
                    f"as '{batch.filename}' (batch {batch.id}, status {batch.status}).",
                    file=sys.stderr,
                )
                return 1
            errors = process_batch(session, batch, result)
    except SQLAlchemyError:
        logger.exception("Could not import %s", result.path)
        print(
            f"ERROR {result.path}: could not import the file (database unavailable).",
            file=sys.stderr,
        )
        return 1

    if batch.status != "COMPLETED":
        print(f"Batch {batch.id} FAILED: {batch.failure_reason}", file=sys.stderr)
        return 1

    print(
        f"Batch {batch.id} COMPLETED: {batch.rows_total} rows, "
        f"{batch.rows_accepted} accepted, {batch.rows_rejected} rejected."
    )
    for error in errors:
        print(f"Row {error.row_number} [{error.column_name}]: {error.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
