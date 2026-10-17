"""File-level checks for uploaded sales CSV files.

Checks existence, extension, size, readability, UTF-8 encoding (an optional
BOM is allowed) and CSV syntax. LF and CRLF line endings are both accepted.
Headers, rows and hashing are handled elsewhere.
"""

import csv
import io
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileCheckResult:
    path: Path
    is_valid: bool
    message: str


def check_csv_file(path: Path, max_size_bytes: int) -> FileCheckResult:
    def fail(message: str) -> FileCheckResult:
        return FileCheckResult(path, is_valid=False, message=message)

    if not path.exists():
        return fail("File does not exist.")
    if not path.is_file():
        return fail("Path is not a regular file.")
    if path.suffix.lower() != ".csv":
        return fail("File must have a .csv extension.")

    try:
        size_bytes = path.stat().st_size
        # Size is checked before reading so oversized files are never loaded.
        if size_bytes == 0:
            return fail("File is empty.")
        if size_bytes > max_size_bytes:
            return fail(
                f"File is {size_bytes} bytes, which exceeds the {max_size_bytes}-byte limit."
            )
        content = path.read_bytes()
    except OSError as error:
        return fail(f"File cannot be read ({error.strerror or 'unknown error'}).")

    try:
        # Decoding as plain UTF-8 (not utf-8-sig) keeps error.start a true file
        # offset even when a BOM is present; the BOM is stripped afterwards.
        text = content.decode("utf-8").removeprefix("﻿")
    except UnicodeDecodeError as error:
        return fail(
            f"File is not valid UTF-8 (invalid byte at position {error.start})."
        )
    if not text:
        return fail("File is empty.")

    syntax_error = find_csv_syntax_error(text)
    if syntax_error:
        return fail(syntax_error)

    return FileCheckResult(path, is_valid=True, message="File passed basic checks.")


def find_csv_syntax_error(text: str) -> str | None:
    # newline="" lets the csv module handle LF, CRLF and newlines inside quotes.
    # strict=True rejects unclosed quotes and text after a closing quote, which
    # the default mode silently accepts.
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        for _ in reader:
            pass
    except csv.Error as error:
        return f"Malformed CSV near line {reader.line_num}: {error}."
    return None


def check_csv_files(paths: list[Path], max_size_bytes: int) -> list[FileCheckResult]:
    return [check_csv_file(path, max_size_bytes) for path in paths]
