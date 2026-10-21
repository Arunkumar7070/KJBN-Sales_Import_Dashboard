"""Pure file and CSV syntax validation (no database dependencies).

This module handles the first gate of ingestion: checking file extension,
file size, UTF-8/BOM decoding, header validation, and converting raw text lines
into structured row dictionaries with all whitespace stripped.
"""

import csv
import hashlib
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# The mandatory 10 columns required by Problem 5 specification
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

# Only returns (negative quantity) are allowed to have an original_transaction_id
OPTIONAL_FIELDS = frozenset({"original_transaction_id"})

# Used as the error column name when the whole row is broken (e.g., column count mismatch)
WHOLE_ROW = "__row__"


@dataclass(frozen=True)
class CsvRow:
    """Represents a successfully parsed row from the CSV file.
    
    row_number: The line number in the spreadsheet (Header is 1, data starts at 2).
    values: A dictionary of column_name -> stripped string value.
    """
    row_number: int
    values: dict[str, str]


@dataclass(frozen=True)
class RowError:
    """Represents a rejected row that failed validation.
    
    row_number: The line number where the bad data was found.
    column_name: The specific field that failed (or '__row__' if column count is wrong).
    message: Human-readable explanation of why the row was rejected.
    transaction_id: The ID of the transaction if it was readable, else None.
    """
    row_number: int
    column_name: str
    message: str
    transaction_id: str | None = None


@dataclass(frozen=True)
class FileCheckResult:
    """The complete outcome of parsing a CSV file.
    
    is_valid: True if the file itself (encoding, syntax, headers) is usable.
    rows: Tuple of valid CsvRows ready for business-rule checks.
    row_errors: Tuple of RowErrors found during basic row checks.
    sha256: The cryptographic hash of the file bytes (used for duplicate detection).
    """
    path: Path
    is_valid: bool
    message: str
    rows: tuple[CsvRow, ...] = ()
    row_errors: tuple[RowError, ...] = ()
    sha256: str | None = None
    is_header_problem: bool = False


class CsvFileError(Exception):
    """Raised when the entire file is unusable (e.g., file not found, bad encoding, empty)."""


class CsvHeaderError(CsvFileError):
    """Raised when the header row is missing, has extra columns, or is misspelled."""


def check_csv_file(path: Path, max_size_bytes: int) -> FileCheckResult:
    """Reads a file from disk and runs all encoding, syntax, and header checks.
    
    Returns a FileCheckResult object. If the file cannot be read or is invalid,
    it returns is_valid=False with a clear error message instead of crashing.
    """
    try:
        content = read_file(path, max_size_bytes)
    except CsvFileError as error:
        return FileCheckResult(path, is_valid=False, message=str(error))
    return check_csv_content(path, content)


def check_csv_content(path: Path, content: bytes) -> FileCheckResult:
    """Validates raw CSV bytes that are already loaded in memory.
    
    Steps:
    1. Decodes bytes to UTF-8 (and strips BOM if present).
    2. Parses CSV dialect and line endings (LF and CRLF).
    3. Validates the header columns against REQUIRED_HEADERS.
    4. Separates valid data rows from structurally broken rows.
    5. Computes SHA-256 hash for idempotency checking.
    """
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
    """Safely reads raw bytes from a file path.
    
    Checks that the file exists, is a regular file, has a .csv extension,
    and is within the allowed size limit BEFORE reading it into memory.
    """
    if not path.exists():
        raise CsvFileError("File does not exist.")
    if not path.is_file():
        raise CsvFileError("Path is not a regular file.")

    try:
        check_name_and_size(path, path.stat().st_size, max_size_bytes)
        return path.read_bytes()
    except OSError as error:
        raise CsvFileError(
            f"File cannot be read ({error.strerror or 'unknown error'})."
        ) from error


def check_name_and_size(path: Path, size_bytes: int, max_size_bytes: int) -> None:
    """Enforces file-level restrictions on name extension and byte length.
    
    Rejects non-csv files, empty files (0 bytes), and files exceeding the limit
    (e.g., 25MB) to protect server memory.
    """
    if path.suffix.lower() != ".csv":
        raise CsvFileError("File must have a .csv extension.")
    if size_bytes == 0:
        raise CsvFileError("File is empty.")
    if size_bytes > max_size_bytes:
        raise CsvFileError(
            f"File is {size_bytes} bytes, which exceeds the {max_size_bytes}-byte limit."
        )


def decode_utf8(content: bytes) -> str:
    """Decodes raw bytes into a UTF-8 string and removes the UTF-8 BOM if present.
    
    The Problem 5 spec specifically tests for UTF-8 BOM (Byte Order Mark: '\\xef\\xbb\\xbf').
    This function removes it cleanly so it does not corrupt the first column header.
    """
    try:
        text = content.decode("utf-8").removeprefix("\ufeff")
    except UnicodeDecodeError as error:
        raise CsvFileError(
            f"File is not valid UTF-8 (invalid byte at position {error.start})."
        ) from error
    if not text:
        raise CsvFileError("File is empty.")
    return text


def parse_records(text: str) -> list[list[str]]:
    """Splits CSV string into rows using Python's standard csv.reader.
    
    Using newline='' handles both Unix (\\n) and Windows (\\r\\n) line endings,
    as well as quoted values containing linebreaks.
    """
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        return list(reader)
    except csv.Error as error:
        raise CsvFileError(f"Malformed CSV near line {reader.line_num}: {error}.") from error


def validate_rows(
    header: list[str], records: list[list[str]]
) -> tuple[tuple[CsvRow, ...], tuple[RowError, ...]]:
    """Iterates through all CSV records and runs basic structural checks on each.
    
    Ignores completely blank lines and tracks spreadsheet-accurate row numbers (header is 1).
    Returns (accepted_rows, rejected_errors).
    """
    rows, errors = [], []
    for row_number, record in enumerate(records, start=2):
        if not record:
            continue
        outcome = validate_row(header, record, row_number)
        (errors if isinstance(outcome, RowError) else rows).append(outcome)
    return tuple(rows), tuple(errors)


def validate_row(
    header: list[str], record: list[str], row_number: int
) -> CsvRow | RowError:
    """Validates a single data line against the CSV header.
    
    1. Checks if the row has the exact number of columns as the header.
    2. Strips all leading/trailing whitespace from every field.
    3. Verifies that mandatory columns are not empty.
    Returns a CsvRow if valid, or a RowError if broken.
    """
    if len(record) != len(header):
        return RowError(
            row_number,
            WHOLE_ROW,
            f"Row has {len(record)} column(s); expected {len(header)}.",
        )

    # Strips leading and trailing whitespaces from every cell right at the gate
    values = {name: value.strip() for name, value in zip(header, record, strict=True)}
    missing = [
        name
        for name in REQUIRED_HEADERS
        if name not in OPTIONAL_FIELDS and not values[name]
    ]
    if missing:
        return RowError(
            row_number,
            missing[0],
            f"Missing required value(s): {', '.join(missing)}.",
            transaction_id=values.get("transaction_id") or None,
        )
    return CsvRow(row_number, values)


def find_header_problem(
    header: Sequence[str] | None, required: Sequence[str] = REQUIRED_HEADERS
) -> str | None:
    """Validates that the CSV header row has exactly all required column names.
    
    Detects missing columns, unexpected columns, duplicates, and blank column titles.
    Returns an error description string if invalid, or None if the header is valid.
    """
    if not header:
        return "Header row is missing."

    names = [name for name in header if name.strip()]
    blank_positions = [
        str(position) for position, name in enumerate(header, 1) if not name.strip()
    ]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    missing = [name for name in required if name not in names]
    unexpected = [
        describe_unexpected(name, required)
        for name in dict.fromkeys(names)
        if name not in required
    ]

    problems = []
    if missing:
        problems.append(f"missing column(s): {', '.join(missing)}")
    if unexpected:
        problems.append(f"unexpected column(s): {', '.join(unexpected)}")
    if duplicates:
        problems.append(f"duplicate column(s): {', '.join(duplicates)}")
    if blank_positions:
        problems.append(f"blank column name(s) at position(s): {', '.join(blank_positions)}")
    return f"Invalid header: {'; '.join(problems)}." if problems else None


def describe_unexpected(name: str, required: Sequence[str] = REQUIRED_HEADERS) -> str:
    """Helper that provides helpful error messages for near-miss column names.
    
    For example: if header has 'SKU' instead of 'sku', it suggests the exact fix.
    """
    normalized = name.strip().lower()
    if normalized in required:
        return f"'{name}' (expected '{normalized}')"
    return f"'{name}'"