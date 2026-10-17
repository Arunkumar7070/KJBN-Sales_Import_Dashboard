"""File-level checks for uploaded sales CSV files.

Checks existence, extension, size, readability, UTF-8 encoding (an optional
BOM is allowed), CSV syntax and the header row. LF and CRLF line endings are
both accepted. Columns may appear in any order; rows are mapped by header name.
Only a file that passes every check gets the SHA-256 hash of its original bytes.
Row values and business rules are handled elsewhere.

The command line also records a QUEUED import batch for a valid file, or
reports that the same file (same hash) was already uploaded.

Usage (from backend/):
    python -m app.services.csv_validator sales.csv
"""

import argparse
import csv
import hashlib
import io
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import ImportBatch

logger = logging.getLogger(__name__)

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
        description="Validate a sales CSV file and print its SHA-256 hash."
    )
    parser.add_argument("path", type=Path, help="CSV file path")
    args = parser.parse_args(argv)

    result = check_csv_file(args.path, get_settings().max_upload_size_bytes)
    if not result.is_valid:
        print(f"INVALID {result.path}: {result.message}", file=sys.stderr)
        return 1

    print(f"{result.sha256}  {result.path}")
    print(f"Rows: {len(result.rows)} valid, {len(result.row_errors)} invalid.")
    for error in result.row_errors:
        print(f"Row {error.row_number}: {error.message}")
    return record_batch(result)


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


def record_batch(result: FileCheckResult) -> int:
    if result.sha256 is None:
        raise ValueError("Only a valid file (with a hash) can be recorded as a batch.")
    try:
        with SessionLocal() as session:
            registration = register_import_batch(
                session, filename=result.path.name, file_sha256=result.sha256
            )
            session.commit()
    except SQLAlchemyError:
        logger.exception("Could not record import batch for %s", result.path)
        print(
            f"ERROR {result.path}: could not record the import batch "
            "(database unavailable).",
            file=sys.stderr,
        )
        return 1

    batch = registration.batch
    if not registration.is_new:
        print(
            f"ALREADY UPLOADED {result.path}: this file was already uploaded as "
            f"'{batch.filename}' (batch {batch.id}, status {batch.status}).",
            file=sys.stderr,
        )
        return 1

    print(f"Created import batch {batch.id} with status {batch.status}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
