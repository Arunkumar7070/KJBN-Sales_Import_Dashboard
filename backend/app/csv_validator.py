"""File-level checks for uploaded sales CSV files.

Checks existence, extension, size, readability, UTF-8 encoding (an optional
BOM is allowed), CSV syntax and the header row. LF and CRLF line endings are
both accepted. Columns may appear in any order; rows are mapped by header name.
Row values, hashing and business rules are handled elsewhere.
"""

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class FileCheckResult:
    path: Path
    is_valid: bool
    message: str
    # Data rows keyed by header name; empty unless the file is valid.
    rows: tuple[dict, ...] = ()


class CsvFileError(Exception):
    """A problem that makes the whole file unusable."""


def check_csv_file(path: Path, max_size_bytes: int) -> FileCheckResult:
    try:
        content = read_file(path, max_size_bytes)
        text = decode_utf8(content)
        rows = parse_rows(text)
    except CsvFileError as error:
        return FileCheckResult(path, is_valid=False, message=str(error))
    return FileCheckResult(
        path, is_valid=True, message="File passed basic checks.", rows=rows
    )


def check_csv_files(paths: list[Path], max_size_bytes: int) -> list[FileCheckResult]:
    return [check_csv_file(path, max_size_bytes) for path in paths]


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


def parse_rows(text: str) -> tuple[dict, ...]:
    # newline="" lets the csv module handle LF, CRLF and newlines inside quotes.
    # strict=True rejects unclosed quotes and text after a closing quote, which
    # the default mode silently accepts.
    reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
    try:
        header_problem = find_header_problem(reader.fieldnames)
        if header_problem:
            raise CsvFileError(header_problem)
        # Rows with too few or too many fields are kept as DictReader maps them
        # (missing values are None, extras sit under the None key) for
        # row-level validation to reject later.
        return tuple(reader)
    except csv.Error as error:
        # DictReader.line_num only updates after a successful row, so read the
        # underlying reader's count to report the line that actually failed.
        raise CsvFileError(
            f"Malformed CSV near line {reader.reader.line_num}: {error}."
        ) from error


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
